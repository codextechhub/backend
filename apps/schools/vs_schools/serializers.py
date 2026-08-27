from __future__ import annotations

from django.utils import timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from .models import (
    ContactInfo,
    Currency,
    OwnershipType,
    REQUIRED_PROFILE_FIELDS,
    SchoolPackageSetup,
    InviteStatus,
    RESERVED_TENANT_SLUGS,
    School,
    TermStructure,
    BranchPrimaryAdmin,
    SchoolPrimaryAdmin,
    SchoolBranding,
    SchoolStatus,
    PackagePlan,
    slug_is_reserved,
)
from core.media import signed_url
from vs_tenants.exceptions import BranchAlreadyInState, TenantSlugFrozen
from vs_tenants.models import Branch, BranchLifecycle, BranchStatus, Tenant
from vs_audit.models import AuditModuleKey, AuditActionType, AuditSeverity
from vs_audit.services import AuditDiffService, emit_audit_event
from vs_config.models import Capability, CapabilityEntitlement
from vs_config.services.capabilities import set_entitlement
from vs_user.email_normalization import normalize_email


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


#: Branch states that still occupy a seat on the school's plan. CLOSED is
#: terminal and the site is gone, so a school that shuts Ikeja gets that seat
#: back and can open Yaba in its place; a merely suspended or inactive site is
#: expected to come back and keeps its seat. Counting closed sites would mean a
#: school on a two-site plan could never replace one, only ever pay for more.
BRANCH_STATES_AGAINST_PLAN = [
    BranchStatus.ACTIVE,
    BranchStatus.PENDING,
    BranchStatus.SUSPENDED,
    BranchStatus.INACTIVE,
]


def branch_ceiling_refusal(*, plan, existing: int, adding: int = 1) -> str:
    """The refusal for a plan's branch ceiling, or "" when there is room.

    ``PackagePlan.max_branch`` was stored, seeded with real numbers (Starter 1,
    Standard 5, Premium 20) and shown on the plans screen, and nothing in the
    codebase ever read it: Bright Star signed for one site and opened four,
    which made the ceiling a sales promise the product quietly broke. It is
    checked here rather than in each caller so the two ways a school gains a
    branch - the standalone create endpoint and the nested list inside school
    creation - cannot answer the question differently.

    ``max_branch=None`` is unlimited, which is what the Enterprise plan means by
    it, and a school with no package setup at all has no ceiling to breach.
    """
    limit = getattr(plan, "max_branch", None) if plan else None
    if limit is None:
        return ""
    if existing + adding <= limit:
        return ""
    plan_name = getattr(plan, "name", "") or "this plan"
    seats = "branch" if limit == 1 else "branches"
    if existing:
        have = f"This school already has {existing}."
    else:
        have = f"This request would create {adding}."
    return (
        f"{plan_name} allows {limit} {seats}. {have} "
        f"Upgrade the package plan to add more."
    )


def full_clean_as_field_errors(instance, *, wrote: Optional[Iterable[str]] = None) -> None:
    """``instance.full_clean()``, with its refusal renamed into DRF's shape.

    A model ``ValidationError`` is keyed by field - ``{"_type": ["This field
    cannot be blank."]}`` - but nothing on the update path was translating it,
    so it travelled all the way to ``core.exceptions.custom_exception_handler``
    and came back as the bare sentence with no field attached. The caller was
    told a field was blank and not which one, on an endpoint that writes eight
    of them.

    ``as_serializer_error`` is DRF's own converter: it keeps the field keys and
    moves model-level (``__all__``) errors under ``non_field_errors``, so a
    ``full_clean`` failure lands in exactly the place a caller already reads
    field errors from this endpoint.

    ``wrote`` names the fields the request actually set, and everything else on
    the row is excluded from field validation. A PATCH validating columns
    nobody touched hands the caller somebody else's problem: a Branch stored
    with a blank ``name`` by ``seed_import``, a data migration or the shell
    answers "This field cannot be blank" to a request that only changed the
    address, and the row is then unpatchable through the API for ever - the
    field cannot be corrected either, because correcting it is an update and
    the update is what is being refused. ``Branch._type`` reached exactly that
    dead end (vs_tenants 0007 gave it ``blank=True`` to escape), and giving one
    column a default fixes one column; excluding the untouched ones is what
    stops the next column doing it again.

    Excluding a field also drops it from ``validate_unique`` and
    ``validate_constraints``, which is right rather than merely tolerable: an
    untouched column still holds the value the row was already stored with, so
    there is nothing a re-check could discover, and the database indexes stand
    either way. ``Model.clean()`` runs regardless of ``exclude``, so the
    row-level rules - ``Branch.clean()`` stamping ``closed_at``,
    ``School.clean()`` refusing a reserved slug - are untouched by this.

    Passing no ``wrote`` keeps the old whole-row behaviour, for a caller that
    means to validate the entire instance.
    """
    exclude = None
    if wrote is not None:
        touched = set(wrote)
        exclude = {
            field.name
            for field in instance._meta.concrete_fields
            if field.name not in touched
        }
    try:
        instance.full_clean(exclude=exclude)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(
            serializers.as_serializer_error(exc)
        ) from exc


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
    # A module with an unmet dependency stays OFF even when granted, so the
    # picker needs the required keys to warn the operator (e.g. procurement
    # needs finance).
    dependencies = serializers.SerializerMethodField()

    class Meta:
        model = Capability
        fields = [
            "id",
            "key",
            "label",
            "description",
            "dependencies",
            "is_active",
        ]
        read_only_fields = fields

    def get_dependencies(self, obj):
        return [link.requires.key for link in obj.dependency_links.all()]


class SchoolPackageSetupWriteSerializer(serializers.Serializer):
    """
    Write-only structure for submitting package setup during school creation.

    Accepts `package_plan` as the PackagePlan `code` (slug) - more stable
    than a numeric PK and matches what the dropdown naturally emits.
    Accepts `enabled_modules` as a list of Capability `key` strings.

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
            queryset=Capability.objects.filter(
                is_active=True, kind=Capability.Kind.MODULE
            ),
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
    enabled_modules = serializers.SerializerMethodField()

    def get_enabled_modules(self, obj):
        # Entitlements are tenant-scoped, never school-scoped: the school
        # reaches its own tenant. Filtering on the tenant (not a NULL-tenant
        # platform grant) keeps one school from reading another's package.
        capability_ids = CapabilityEntitlement.all_objects.filter(
            tenant_id=obj.school.tenant_id,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
        ).values_list("capability_id", flat=True)
        capabilities = Capability.objects.filter(pk__in=capability_ids, is_active=True)
        return XVSModuleSerializer(capabilities, many=True).data

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

    def validate_full_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("full_name cannot be empty.")
        return value.strip()


class BranchPrimaryAdminReadSerializer(serializers.Serializer):
    """Read-only view; returns link + contact."""
    id = serializers.CharField()
    branch_role = serializers.CharField()
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

    def validate_full_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("full_name cannot be empty.")
        return value.strip()


class SchoolPrimaryAdminReadSerializer(serializers.Serializer):
    """Read-only view; returns school admin link + contact."""
    id = serializers.CharField()
    school_role = serializers.CharField()
    invite_status = serializers.CharField()
    invite_queued_at = serializers.DateTimeField(allow_null=True)
    invite_sent_at = serializers.DateTimeField(allow_null=True)
    contact = ContactInfoSerializer()


# -----------------------------------------------------------------------------
# Branch serializers (read)
# -----------------------------------------------------------------------------

def branch_school_slug(branch) -> Optional[str]:
    """The slug of the school that owns ``branch``, or ``None``.

    ``Branch`` no longer holds a school foreign key; it reaches its school
    through the tenant that owns both, which is the same value and the same
    wire contract. Two things make this a function rather than a serializer
    ``source="tenant.school_profile.slug"``:

    * ``Branch`` is a platform model now, so a tenant that is not a school can
      own one. The dotted source raises on the missing reverse one-to-one, and
      DRF answers that by dropping the key from the payload rather than by
      nulling it, silently changing the response shape.
    * The value costs a query per row unless the queryset carries
      ``select_related("tenant__school_profile")``. Saying it once here is the
      reminder; the branch querysets in ``views/branch.py`` do carry it.

    Declared on each serializer rather than through a mixin on purpose: DRF's
    ``SerializerMetaclass`` only collects declared fields from bases that are
    themselves serializers, so a field on a plain mixin is silently ignored and
    ``Meta.fields`` then fails with "not valid for model Branch".
    """
    school = getattr(branch.tenant, "school_profile", None)
    return school.slug if school is not None else None


class BranchListSerializer(serializers.ModelSerializer):
    school_slug = serializers.SerializerMethodField()

    def get_school_slug(self, obj) -> Optional[str]:
        return branch_school_slug(obj)

    class Meta:
        model = Branch
        fields = [
            "id",
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
    school_slug = serializers.SerializerMethodField()
    primary_admin = BranchPrimaryAdminReadSerializer(read_only=True)

    def get_school_slug(self, obj) -> Optional[str]:
        return branch_school_slug(obj)

    class Meta:
        model = Branch
        fields = [
            "id",
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
            "activated_at",

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
        from vs_config.platform_settings import get_school_onboarding_defaults

        attrs.setdefault(
            "country", get_school_onboarding_defaults()["branch_country"]
        )
        school = self.context.get("school")
        is_main = attrs.get("is_main", False)
        if school and is_main:
            if Branch.all_objects.filter(tenant=school.tenant, is_main=True).exists():
                raise serializers.ValidationError({"is_main": "This school already has a main branch."})

        # The plan's branch ceiling, checked before anything is written.
        if school:
            setup = SchoolPackageSetup.objects.filter(
                school=school
            ).select_related("package_plan").first()
            if setup:
                refusal = branch_ceiling_refusal(
                    plan=setup.package_plan,
                    # ``all_objects``, not ``objects``: the latter is
                    # tenant-scoped to the caller, and the caller here is
                    # usually a CodeX operator adding a branch on the school's
                    # behalf. Scoped, they would count zero sites and every
                    # plan would look empty.
                    existing=Branch.all_objects.filter(
                        tenant=school.tenant,
                        status__in=BRANCH_STATES_AGAINST_PLAN,
                    ).count(),
                )
                if refusal:
                    raise serializers.ValidationError({"non_field_errors": [refusal]})

        primary_admin_data = attrs.get("primary_admin_data")
        if primary_admin_data:
            from vs_user.services.email_availability import email_refusal
            # Scoped to the school this branch is being added to, via the one
            # helper every creation path now shares, so all of them agree on
            # what "already exists" means. They did not agree twice over: this
            # path once compared case-sensitively while vs_user compared with
            # iexact, and then both asked about the whole platform after
            # uniqueness had narrowed to one address per tenant.
            refusal = email_refusal(
                primary_admin_data.get("email"),
                tenant=school.tenant if school else None,
            )
            if refusal:
                raise serializers.ValidationError({
                    "primary_admin_data": {"email": refusal}
                })

        return attrs

    @transaction.atomic
    def create(self, validated_data: Dict[str, Any]) -> Branch:
        primary_admin_data = validated_data.pop("primary_admin_data", None)
        school = self.context.get("school")
        
        # Set default lifecycle state if you want it always created as pending
        # ``tenant`` is supplied explicitly: Branch no longer has a school to
        # derive it from, so every creation path has to name the owner.
        branch = Branch.objects.create(
            tenant=school.tenant,
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

        from .models import BranchPrimaryAdmin
        from .services.admin_provisioning import provision_admin_user
        from vs_rbac.services import provision_role_from_prebuilt

        # Provision branch_admin role template (branch-scoped) before any email is sent
        branch_admin_role = provision_role_from_prebuilt(
            tenant=school.tenant,
            branch=branch,
            prebuilt_key="branch_admin",
            created_by=self.context.get("actor_id"),
        )

        # Optional primary admin assignment (ContactInfo + link)
        if primary_admin_data:
            contact = ContactInfo.objects.create(
                full_name=primary_admin_data["full_name"],
                email=primary_admin_data["email"],
                phone=primary_admin_data.get("phone", ""),
            )
            admin_link = BranchPrimaryAdmin.objects.create(
                branch=branch,
                contact=contact,
                branch_role=primary_admin_data.get("branch_role", "Head Teacher"),
                invite_status=InviteStatus.QUEUED,
                invite_queued_at=timezone.now(),
                invite_sent_at=None,
            )
            provision_admin_user(
                contact=contact,
                admin_link=admin_link,
                school=school,
                branch=branch,
                role=branch_admin_role.key if branch_admin_role else "",
                actor=self.context.get("actor_id"),
            )
        else:
            raise serializers.ValidationError({"primary_admin_data": "Primary admin information is required to create a branch."})
        
        _snap = AuditDiffService.from_instances(
            before_instance=None,
            after_instance=branch,
            exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deactivated_at"],
        )
        # Keyed on the primary key, not the code. ``Branch.code`` is allocated
        # per tenant from 1, so every school's main branch is code 1 and
        # ``EntityAuditTrail`` is unique on (entity_type, entity_id) with no
        # tenant column: a code-keyed trail put Bright Star's, Greenfield's and
        # Corona's main branches on one platform-wide row, interleaved, with
        # nothing on the event saying whose branch it was. The pk is unique
        # across tenants and a branch cannot change it.
        #
        # The code still has to be findable. It is what a school's own staff
        # call the branch ("branch 2"), and it is no longer in ``entity_id``
        # where the Event Explorer's free-text search would reach it - worse,
        # searching "2" there now finds the branch whose *pk* is 2, which is
        # somebody else's. The summary carries it, together with the school,
        # because "Main Branch" is not a distinguishing label either.
        emit_audit_event(
            module_key=AuditModuleKey.BRANCH,
            action_type=AuditActionType.CREATE,
            actor_user=self.context.get("actor_id"),
            # ``branch.tenant``, not ``school.tenant``, and they are the same
            # row: ``Branch`` is owned directly by ``Tenant`` and this create
            # passed ``tenant=school.tenant`` a few lines up. Reading it off
            # the branch says what the audit row means - the tenant this
            # BRANCH belongs to - and keeps working if the branch ever arrives
            # from somewhere the school is not in scope.
            #
            # Without it the row landed with tenant NULL, and an investigator
            # asking "show me everything at Bright Star" had to read summaries
            # and labels instead of filtering a column.
            tenant=branch.tenant,
            entity_type="Branch",
            entity_id=str(branch.pk),
            entity_label=branch.name,
            summary=f"{branch.name} created as branch {branch.code} of {school.name}",
            before_data=_snap["before_data"],
            diff_data=_snap["diff"],
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
            "_type",

            "address",
            "email",

            "country",
            "state",
            "opened_at",
        ]

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        branch: Branch = self.instance
        # ``is_main=true`` used to be refused whenever another main branch
        # existed, which made promotion impossible for every school that had
        # one - and a main branch can no longer be retired without promoting a
        # sibling first, so that refusal was the dead end itself. It is now a
        # handover: ``Branch.promote_to_main`` demotes the incumbent in the
        # same transaction. All that is refused here is promoting a branch that
        # is out of service.
        if attrs.get("is_main") is True and branch.status not in Branch.IN_SERVICE_STATES:
            raise serializers.ValidationError({
                "is_main": (
                    f"This branch is {branch.status} and cannot become the main "
                    f"branch. Bring it back into service first."
                )
            })
        # Clearing the flag outright would leave the school with no main branch
        # at all, which is the same damage by another route: promote the
        # successor instead and the incumbent is demoted for you.
        if attrs.get("is_main") is False and branch.is_main:
            raise serializers.ValidationError({
                "is_main": (
                    "A school must always have a main branch. Make another "
                    "branch the main branch instead; this one is demoted "
                    "automatically."
                )
            })
        return attrs

    @transaction.atomic
    def update(self, instance: Branch, validated_data: Dict[str, Any]) -> Branch:
        before_instance = AuditDiffService.model_instance_to_dict(
            instance,
            exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deactivated_at"],
        )

        # Every field this request named, including the ``is_main`` popped just
        # below: what full_clean is allowed to look at is decided by what the
        # caller sent, not by which of those values turned out to differ.
        wrote = set(validated_data)

        # Handled by promote_to_main, not by a plain attribute write: the
        # incumbent has to be demoted first or the partial unique index refuses
        # the promotion even though the end state is legal.
        promoting = validated_data.pop("is_main", None) is True and not instance.is_main

        changes = 1 if promoting else 0
        for attr, value in validated_data.items():
            if getattr(instance, attr) != value:
                changes += 1
                setattr(instance, attr, value)

        if changes == 0:
            raise serializers.ValidationError({"detail": "No changes detected in update payload."})

        full_clean_as_field_errors(instance, wrote=wrote)
        instance.save()

        if promoting:
            instance.promote_to_main(actor_id=self.context.get("actor_id"))

        after_instance = AuditDiffService.model_instance_to_dict(
            instance,
            exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deactivated_at"],
        )

        emit_audit_event(
            module_key=AuditModuleKey.BRANCH,
            action_type=AuditActionType.UPDATE,
            actor_user=self.context.get("actor_id"),
            # The branch owns its tenant directly; see the create path.
            tenant=instance.tenant,
            entity_type="Branch",
            # The pk, matching the create path, so an edit lands on this
            # branch's own trail instead of the one row every school's branch
            # of the same code used to share. The code itself is not repeated
            # here: it is ``editable=False`` and never changes, so naming it
            # once on the creation event keeps it findable for good.
            entity_id=str(instance.pk),
            entity_label=instance.name,
            before_data=before_instance,
            diff_data=AuditDiffService.diff_dicts(
                before_data=before_instance,
                after_data=after_instance,
            ),
        )

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
            # pk is what scoped endpoints (vs_config entitlements/overrides,
            # notification settings ?school=) take - pickers need it.
            "id",
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
    total_students = serializers.ReadOnlyField(default=0)

    class Meta:
        model = School
        fields = [
            # The list has carried the pk all along; the detail response is the
            # one a console screen is actually built from, and it could not
            # name the school it was showing. Audit trails are keyed on the pk
            # now, so "view this school's audit trail" needs it, as do the
            # tenant-scoped endpoints (vs_config entitlements and overrides,
            # notification settings) that take an id rather than a slug.
            "id",
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
    used as a nested write structure - the actual Branch model creation happens
    inside SchoolCreateSerializer.create(), not here.

    Each branch entry must include primary_admin_data.
    is_main defaults to False. Exactly one branch must end up is_main=True; when
    a school is created with a single branch, that branch is promoted to main
    because it is the only site the school has.
    """

    name = serializers.CharField(max_length=255)
    # Optional, to match the column and the other branch write paths. This was
    # the one place a free-form descriptor was mandatory, and it made a school
    # impossible to create over an import row that had no branch type.
    _type = serializers.CharField(
        max_length=80, required=False, allow_blank=True, default="",
    )
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    country = serializers.CharField(max_length=80, required=False)
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
    Creates a School with its branches, plus optional:
      - Branding
      - School-level primary admin
      - Package setup

    The `branches` field accepts a list of branch objects and is **required**:
    every school has at least one branch, its main branch, from the moment it
    exists. It used to be ``required=False, default=list``, which let this
    endpoint (and the bulk importer behind it) mint a school with nowhere to put
    a user, a document or a student. Every branch rule below used to sit behind
    an ``if branches:`` and so never ran for the one payload that needed them.

    Business rules enforced here:
      - At least ONE branch must be submitted.
      - Exactly one branch must have is_main=True.
      - Branch names must be unique within the submission.
    """

    slug = serializers.CharField(required=False, allow_blank=True)
    branding = SchoolBrandingSerializer(required=False)
    primary_admin_data = SchoolPrimaryAdminWriteSerializer(required=False, write_only=True)
    branches = BranchInlineCreateSerializer(
        many=True,
        required=True,
        allow_empty=False,
        write_only=True,
        error_messages={
            "required": "A school must be created with at least one branch, its main branch.",
            "empty": "A school must be created with at least one branch, its main branch.",
            "null": "A school must be created with at least one branch, its main branch.",
        },
    )
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
        from vs_config.platform_settings import get_school_onboarding_defaults

        onboarding_defaults = get_school_onboarding_defaults()
        attrs.setdefault("ownership_type", onboarding_defaults["ownership_type"])
        attrs.setdefault("term_structure", onboarding_defaults["term_structure"])
        attrs.setdefault("currency", onboarding_defaults["currency"])
        for branch in attrs.get("branches", []):
            branch.setdefault("country", onboarding_defaults["branch_country"])

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

        # --- Admin email existence checks (single DB query) ---
        #
        # The school being created has no tenant row yet, so there is no tenant
        # for an address to be taken IN: the same-tenant rule is vacuous here
        # by construction, and this check exists only to give a readable error
        # for the transitional refusal in ``User._guard_cross_tenant_email``
        # while sign-in does not yet name its tenant. When that switch flips
        # this check goes quiet on its own, and Greenfield can be created with
        # ada.okoye@example.test even though Bright Star already uses it.
        from vs_user.services.email_availability import email_refusals

        primary_admin_data = attrs.get("primary_admin_data", None)
        branches = attrs.get("branches", [])

        _tagged_emails: Dict[str, str] = {}
        if primary_admin_data:
            sa_email = normalize_email(primary_admin_data.get("email"))
            if sa_email:
                _tagged_emails["__school__"] = sa_email

        for i, branch in enumerate(branches):
            ba_data = branch.get("primary_admin_data") or {}
            ba_email = normalize_email(ba_data.get("email"))
            if ba_email:
                _tagged_emails[i] = ba_email

        if _tagged_emails:
            refused = email_refusals(_tagged_emails.values(), tenant=None)
            email_errors: Dict[str, Any] = {}
            sa_refusal = refused.get(_tagged_emails.get("__school__", ""), "")
            if sa_refusal:
                email_errors["primary_admin_data"] = {"email": sa_refusal}
            branch_email_errors = {
                i: {"primary_admin_data": {"email": refused[email]}}
                for i, email in _tagged_emails.items()
                if i != "__school__" and email in refused
            }
            if branch_email_errors:
                email_errors["branches"] = branch_email_errors
            if email_errors:
                raise serializers.ValidationError(email_errors)

        # --- Branch-level validations ---
        # No longer behind ``if branches:``. The field is required and rejects an
        # empty list, so reaching here means at least one branch was submitted,
        # and these rules run for every school that is ever created.
        if not branches:
            raise serializers.ValidationError({
                "branches": "A school must be created with at least one branch, its main branch."
            })

        # Rule 1: Branch names must be unique within the submission
        names = [b["name"].strip().lower() for b in branches]
        if len(names) != len(set(names)):
            raise serializers.ValidationError({
                "branches": "Each branch must have a unique name within this submission."
            })

        # Rule 2: Exactly one branch must be marked as main
        main_branches = [b for b in branches if b.get("is_main", False)]
        if len(main_branches) == 0:
            if len(branches) == 1:
                # The lone branch of a new school is its main branch. Making the
                # caller say so twice buys nothing.
                branches[0]["is_main"] = True
            else:
                raise serializers.ValidationError({
                    "branches": "Exactly one branch must be marked as is_main=true."
                })
        if len(main_branches) > 1:
            raise serializers.ValidationError({
                "branches": "Only one branch can be marked as is_main=true."
            })

        # Rule 3: the submitted branches must fit the plan being signed for.
        # Checked here as well as on the standalone branch endpoint, because
        # onboarding is the one moment a school can arrive with several sites
        # at once - refusing the fourth branch later while letting all four in
        # at creation would leave the ceiling enforced everywhere except the
        # one path that can breach it in a single request.
        package_setup_data = attrs.get("package_setup_data") or {}
        refusal = branch_ceiling_refusal(
            plan=package_setup_data.get("package_plan"),
            existing=0,
            adding=len(branches),
        )
        if refusal:
            raise serializers.ValidationError({"branches": refusal})

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
            status=SchoolStatus.PENDING,
        )

        # --- 2. Optional branding ---
        if branding_data:
            SchoolBranding.objects.create(school=school, **branding_data)

        from .services.admin_provisioning import provision_admin_user
        from vs_rbac.services import provision_role_from_prebuilt

        actor = self.context["request"].user

        # Provision school_admin role template for this school before any email is sent
        school_admin_role = provision_role_from_prebuilt(
            tenant=school.tenant,
            branch=None,
            prebuilt_key="school_admin",
            created_by=actor,
        )

        school_admin_email = None

        # --- 3. Optional school-level primary admin ---
        if primary_admin_data:
            contact = ContactInfo.objects.create(
                full_name=primary_admin_data["full_name"],
                email=primary_admin_data["email"],
                phone=primary_admin_data.get("phone", ""),
            )
            school_admin_link = SchoolPrimaryAdmin.objects.create(
                school=school,
                contact=contact,
                school_role=primary_admin_data.get("school_role", "IT Head"),
                invite_status=InviteStatus.QUEUED,
                invite_queued_at=timezone.now(),
                invite_sent_at=None,
            )
            school_admin_email = normalize_email(primary_admin_data["email"])
            provision_admin_user(
                contact=contact,
                admin_link=school_admin_link,
                school=school,
                branch=None,
                role=school_admin_role.key if school_admin_role else "",
                actor=actor,
            )

        # --- 4. Create branches inline ---
        for branch_data in branches_data:
            branch_admin_data = branch_data.pop("primary_admin_data", None)

            branch = Branch.objects.create(
                tenant=school.tenant,
                status=BranchStatus.PENDING,
                opened_at=branch_data.pop("opened_at", None) or timezone.now(),
                **branch_data,
            )

            # Provision branch_admin role template (branch-scoped) before any email is sent
            branch_admin_role = provision_role_from_prebuilt(
                tenant=school.tenant,
                branch=branch,
                prebuilt_key="branch_admin",
                created_by=actor,
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
                branch_admin_email = normalize_email(branch_admin_data["email"])
                contact = ContactInfo.objects.create(
                    full_name=branch_admin_data["full_name"],
                    email=branch_admin_data["email"],
                    phone=branch_admin_data.get("phone", ""),
                )
                branch_admin_link = BranchPrimaryAdmin.objects.create(
                    branch=branch,
                    contact=contact,
                    branch_role=branch_admin_data.get("branch_role", "Head Teacher"),
                    invite_status=InviteStatus.QUEUED,
                    invite_queued_at=timezone.now(),
                    invite_sent_at=None,
                )
                if school_admin_email and branch_admin_email == school_admin_email:
                    # Same person as school admin - link is recorded but no new user or email
                    branch_admin_link.invite_status = InviteStatus.SENT
                    branch_admin_link.invite_sent_at = timezone.now()
                    branch_admin_link.save(update_fields=["invite_status", "invite_sent_at"])
                else:
                    provision_admin_user(
                        contact=contact,
                        admin_link=branch_admin_link,
                        school=school,
                        branch=branch,
                        role=branch_admin_role.key if branch_admin_role else "",
                        actor=actor,
                    )
        
            # branch audit trail for creation
            _branch_snap = AuditDiffService.from_instances(
                before_instance=None,
                after_instance=branch,
                exclude_fields=["created_at", "updated_at", "activated_at", "closed_at", "deactivated_at"],
            )
            emit_audit_event(
                module_key=AuditModuleKey.BRANCH,
                action_type=AuditActionType.CREATE,
                # Not context["actor_id"]: the API views put a User in it but
                # the bulk importer puts str(user.id), and a string here makes
                # emit_audit_event swallow the event and write nothing.
                actor_user=actor,
                # Same row as school.tenant - the branch was created with it
                # immediately above - read off the branch for the same reason
                # BranchCreateSerializer does.
                tenant=branch.tenant,
                entity_type="Branch",
                # The pk, not the code: codes restart at 1 for every tenant, so
                # this is the site that used to file every school's main branch
                # under one shared trail. See BranchCreateSerializer.create for
                # the full reasoning, including why the summary names the code.
                entity_id=str(branch.pk),
                entity_label=branch.name,
                summary=f"{branch.name} created as branch {branch.code} of {school.name}",
                before_data=_branch_snap["before_data"],
                diff_data=_branch_snap["diff"],
            )
        
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

            # Close the grant set under capability dependencies (transitively):
            # a picked module must not end up entitled-but-off because its
            # requirement wasn't ticked (e.g. procurement needs finance). The
            # wizard mirrors this expansion client-side; this is the guarantee.
            to_grant = {c.pk: c for c in enabled_modules}
            stack = list(enabled_modules)
            while stack:
                for link in stack.pop().dependency_links.select_related("requires"):
                    required = link.requires
                    if required.pk not in to_grant and required.is_active:
                        to_grant[required.pk] = required
                        stack.append(required)

            # vs_config owns entitlement writes: it computes the canonical
            # "tenant:<id>" scope key and audits every grant. Writing rows
            # here directly is what let this path drift out of step with it.
            for capability in to_grant.values():
                set_entitlement(
                    capability=capability,
                    tenant=school.tenant,
                    state=CapabilityEntitlement.State.GRANTED,
                    source=CapabilityEntitlement.Source.PACKAGE,
                    actor=actor,
                    reason=f"School package setup for {school.name}",
                )

        # --- 6. Set of books (best effort, never fatal) ---
        # Every school gets books, entitled to finance or not: adding them later
        # means going back to repair every school created before this point. The
        # service opens its own savepoint and swallows its own failures, so a
        # finance problem cannot cost us the school, the tenant, the admin user,
        # the branches or the entitlements above.
        from .services.books import provision_books_for_school

        provision_books_for_school(school)

        # --- 7. Onboarding control room (best effort, never fatal) ---
        # A school that cannot see its checklist the moment it is created has
        # to be found and repaired by hand, so provisioning happens here rather
        # than on the school's first sign-in. Same shape as the books above:
        # the service opens its own savepoint and swallows its own failures, so
        # a checklist problem cannot cost us the school. This is the choke point
        # for both creation paths - the bulk importer runs this same serializer.
        from schools.vs_onboarding.services.provisioning import (
            provision_onboarding_for_school,
        )

        provision_onboarding_for_school(school, actor=actor)

        # --- 8. Audit trail for school ---
        _school_snap = AuditDiffService.from_instances(
            before_instance=None,
            after_instance=school,
            exclude_fields=["created_at", "updated_at", "activated_at", "deactivated_at"],
        )
        # Keyed on the primary key, not the slug. A school's slug is editable
        # right up to go-live, and a slug-keyed trail splits down the middle
        # the moment it is corrected: the creation event stays filed under
        # ``bright-star`` while the rename and everything after it file under
        # ``bright-star-academy``, so whoever opens the trail for the address
        # the school actually uses never sees it being created. The pk is the
        # one identifier a school cannot change.
        #
        # The slug still has to be findable, because it is the address people
        # hold and search on, and it is no longer sitting in ``entity_id``
        # where the Event Explorer's free-text search would reach it. Naming
        # it in the summary is the same device the rename event uses.
        emit_audit_event(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.CREATE,
            actor_user=actor,
            # The school's own tenant, so "everything at Bright Star" is a
            # column filter rather than a search through summaries.
            tenant=school.tenant,
            entity_type="School",
            entity_id=str(school.pk),
            entity_label=school.name,
            summary=f"{school.name} created with sign-in address {school.slug}",
            before_data=_school_snap["before_data"],
            diff_data=_school_snap["diff"],
        )

        return school
    

class SchoolUpdateSerializer(serializers.ModelSerializer):
    """
    Updates tenant identity fields only.
    Branch details are updated via BranchUpdateSerializer.

    ``slug`` is writable because a school's address is editable right up to
    go-live and frozen for ever after (``School._check_slug_change``). Without
    it on this serializer the correction that rule exists to permit - Bright
    Star created as ``bright-star`` when its admins meant
    ``bright-star-academy`` - could only be made from a shell.

    ``name`` deliberately is not writable here. It is not covered by the
    address rule, and it is not free of consequences either: the spreadsheet
    importer identifies a school by name when the row carries no slug
    (``vs_import_data.services.import_executor``), so a rename silently turns a
    school's own import file into a request to create a second school. That
    needs its own decision, not a field quietly added beside this one.

    Declared explicitly rather than left to ``ModelSerializer``: the model's
    ``SlugField`` validator refuses anything that is not already lowercase and
    hyphenated, which would turn "Bright Star" into a regex complaint. The
    create path normalises instead, and correcting a typo should behave the
    same way.
    """

    slug = serializers.CharField(required=False)
    branding = SchoolBrandingSerializer(required=False)

    class Meta:
        model = School
        fields = [
            "slug",
            "ownership_type",
            "address",
            "website",
            "motto",
            "term_structure",
            "currency",
            "registration_id",

            # optional nested
            "branding",
        ]

    def validate_slug(self, value: str) -> str:
        """Settle the address here, so no refusal has to escape from ``save()``.

        Every check below has a backstop deeper down - ``School.clean()`` for
        the reserved list, the unique index for the collision,
        ``_check_slug_change()`` for the freeze - and every one of those
        backstops raises where the caller would receive it as a 500 or as a
        stray sentence. They stay where they are, because a shell or a data
        migration must still be refused; this is the same set of rules stated
        where an HTTP caller can be answered properly.
        """
        normalized = _normalize_slug(value)
        if not normalized:
            raise serializers.ValidationError(
                "Provide an address made of lowercase letters, numbers and hyphens."
            )
        if normalized == self.instance.slug:
            return normalized

        # First, because it outranks the rest: a live school may not move to a
        # free slug either, so "that one is taken" would be misleading advice.
        if self.instance.has_ever_been_live():
            raise TenantSlugFrozen(
                tenant_name=self.instance.name, slug=self.instance.slug,
            )

        if slug_is_reserved(normalized):
            raise serializers.ValidationError("This slug is reserved. Choose another.")

        if not _slug_is_unique(normalized, exclude_school_slug=self.instance.slug):
            raise serializers.ValidationError({
                "message": "Slug already exists.",
                "suggestions": [
                    s for s in _build_slug_suggestions(normalized) if _slug_is_unique(s)
                ],
            })

        # The school's slug is mirrored onto its tenant by ``School.save()``,
        # and that mirror is a queryset ``update()`` - it cannot raise a field
        # error, only an IntegrityError against the tenant's own unique index.
        # A clinic group or an ORGANIZATION tenant holding the name is enough
        # to trigger it, and there is no school row to have caught it above.
        if Tenant.objects.filter(slug=normalized).exclude(
            pk=self.instance.tenant_id
        ).exists():
            raise serializers.ValidationError(
                "This address is already in use on the platform. Choose another."
            )

        return normalized

    @transaction.atomic
    def update(self, instance: School, validated_data: Dict[str, Any]) -> School:
        """Write the change, then record who made it.

        This used to read ``actor_id`` out of the context and drop it on the
        floor: no audit event was emitted at all, while ``BranchUpdateSerializer``
        directly above audits every field it touches. That was survivable while
        the endpoint edited mottos. It stopped being survivable at 0699ada, when
        ``slug`` became writable here: the slug is mirrored onto the tenant and
        is therefore the host every one of a school's users signs in at, and it
        could be moved with no record of who moved it or where from.

        Same shape as the branch above, deliberately: the same
        ``AuditDiffService`` snapshot, the same before/diff pair, the same
        ``emit_audit_event`` call, and the same actor resolution - ``.get()``
        with no default, so an absent actor arrives as ``None`` and the event is
        attributed to SYSTEM. The old ``"system"`` string default would have been
        worse than nothing: ``actor_user`` is a FK, a string there raises inside
        ``emit_audit_event``, and that helper swallows its own failures - so a
        defaulted actor meant no event at all rather than a system-attributed one.
        """
        branding_data = validated_data.pop("branding", None)

        before_data = AuditDiffService.model_instance_to_dict(
            instance,
            exclude_fields=["created_at", "updated_at", "activated_at", "deactivated_at"],
        )

        # Same rule as the branch above: full_clean sees the fields this
        # request named and nothing else, so a column left blank by some other
        # path cannot refuse an edit that never went near it.
        wrote = set(validated_data)

        changes = 0
        for attr, value in validated_data.items():
            if getattr(instance, attr) != value:
                changes += 1
                setattr(instance, attr, value)

        if changes == 0:
            raise serializers.ValidationError({"detail": "No changes detected in update payload."})

        full_clean_as_field_errors(instance, wrote=wrote)
        instance.save()

        # Branding upsert
        if branding_data is not None:
            SchoolBranding.objects.update_or_create(
                school=instance,
                defaults=branding_data,
            )

        after_data = AuditDiffService.model_instance_to_dict(
            instance,
            exclude_fields=["created_at", "updated_at", "activated_at", "deactivated_at"],
        )

        # The address move gets its own sentence and its own severity. The old
        # slug is already recoverable from ``before_data`` and from the diff,
        # but neither is searchable: the Event Explorer's free-text search runs
        # over ``summary``, so naming both addresses there is what lets someone
        # holding the dead address find out where the school went. Everything
        # else keeps the generated "{actor} updated School {entity}" summary.
        previous_slug = before_data.get("slug")
        slug_moved = previous_slug != instance.slug
        summary = ""
        if slug_moved:
            summary = (
                f"Sign-in address for {instance.name} moved from "
                f"{previous_slug} to {instance.slug}"
            )

        # Best effort, and not silent: ``emit_audit_event`` never raises and
        # logs its own failures to the ``vs_audit`` logger, which is the audit
        # app's stated contract ("audit failures must never block business
        # logic") and what the branch serializer above relies on. A failed
        # audit write therefore leaves the school edit standing rather than
        # rolling back a legitimate correction over a logging fault.
        emit_audit_event(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.UPDATE,
            actor_user=self.context.get("actor_id"),
            tenant=instance.tenant,
            entity_type="School",
            # The pk, and emphatically not the slug: this is the one call site
            # that can change a school's slug, so keying the event on it would
            # file the rename under the new address and leave everything before
            # it under the old one. See the create path for the full reasoning.
            entity_id=str(instance.pk),
            entity_label=instance.name,
            severity=AuditSeverity.WARNING if slug_moved else AuditSeverity.INFO,
            summary=summary,
            before_data=before_data,
            diff_data=AuditDiffService.diff_dicts(
                before_data=before_data,
                after_data=after_data,
            ),
        )

        return instance


# -----------------------------------------------------------------------------
# Lifecycle & Operations serializers (write)
# -----------------------------------------------------------------------------

# PENDING is deliberately absent: it is the provisioning state, and nothing may
# travel back to "pending activation". See Branch.ALLOWED_TRANSITIONS for the
# edges between the states below.
Branch_Transition_Choice = [
    ("ACTIVE", "Branch Activated"),
    ("SUSPENDED", "Branch Suspended"),
    ("INACTIVE", "Branch Deactivated"),
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
            raise serializers.ValidationError({"to_state": f"Invalid to_state: {to_state}"})

        # transition() treats this as an idempotent no-op; over the API it is
        # worth telling the caller their request changed nothing.
        if branch.status == to_state:
            raise BranchAlreadyInState(state=to_state)

        # Raises InvalidBranchTransition (409) for any edge the lifecycle
        # disallows, e.g. reopening a CLOSED branch.
        branch.transition(to_state=to_state, actor_id=actor_id, reason=reason)

        return branch


#: The two states this endpoint may write. PENDING and SUSPENDED are absent
#: deliberately: they belong to onboarding, and are reached by going live, by
#: the expiry sweep and by reinstatement.
School_Service_State_Choice = [
    ("ACTIVE", "Returned to service"),
    ("INACTIVE", "Taken out of service"),
]


class SchoolServiceStateSerializer(serializers.Serializer):
    """Take a live school out of service, or bring an inactive one back.

    This is the action the branch lifecycle has been pointing at all along:
    ``LastBranchCannotLeaveService`` tells the operator to deactivate the
    school instead of its only branch, and until now nothing could.
    """

    to_state = serializers.ChoiceField(choices=School_Service_State_Choice)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    @transaction.atomic
    def save(self, **kwargs) -> School:
        school: School = self.context["school"]
        actor = self.context.get("actor_id")
        to_state = self.validated_data["to_state"]
        reason = (self.validated_data.get("reason") or "").strip()

        before_data = AuditDiffService.model_instance_to_dict(
            school,
            exclude_fields=["created_at", "updated_at"],
        )
        # Every refusal lives on the model, so a shell or a data migration is
        # held to the same rules as an HTTP caller.
        previous = school.change_service_state(
            to_state=to_state, actor=actor, reason=reason,
        )
        school.refresh_from_db()

        after_data = AuditDiffService.model_instance_to_dict(
            school,
            exclude_fields=["created_at", "updated_at"],
        )
        went_dark = to_state == "INACTIVE"
        emit_audit_event(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.UPDATE,
            actor_user=actor,
            tenant=school.tenant,
            entity_type="School",
            # The pk, so the row lands on the same trail as every other change
            # to this school and survives a change of address.
            entity_id=str(school.pk),
            entity_label=school.name,
            summary=(
                f"{school.name} was taken out of service: {reason}"
                if went_dark
                else f"{school.name} was returned to service"
            ),
            before_data=before_data,
            diff_data=AuditDiffService.diff_dicts(
                before_data=before_data, after_data=after_data,
            ),
        )
        # Returned for the caller's benefit; the audit row already has it.
        self.previous_status = previous
        return school


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

        # The token has to be this school's address, and nothing else will do.
        # It used to be checked for emptiness only, so "x" reset any school an
        # operator could name: with Bright Star open in one tab and Greenfield
        # in another, a reset fired at the wrong slug went through, Bright
        # Star's branding was deleted, and nothing in the request had ever said
        # "Bright Star" for anyone to catch it. Naming the school in the body
        # is what makes a mis-aimed reset a 400 instead of a deletion, and the
        # slug is the right thing to name because it is the address the
        # operator is already looking at in the URL.
        #
        # Compared case-insensitively because slugs are stored lowercase and a
        # typed capital is not a different school; whitespace is stripped for
        # the same reason. Neither loosens what the token has to be.
        token = (self.validated_data.get("confirmation_token") or "").strip()
        if not token:
            raise serializers.ValidationError({"confirmation_token": "Confirmation token is required."})
        if token.casefold() != (school.slug or "").casefold():
            raise serializers.ValidationError({
                "confirmation_token": (
                    f"Type this school's address ({school.slug}) to confirm the reset."
                )
            })

        # Baseline reset example:
        # - Remove branding
        # - Disable all modules (or re-seed defaults depending on your product policy)
        # - Clear localization (optional; many teams keep localization)
        branding = SchoolBranding.objects.filter(school=school).first()
        # Built by hand rather than through ``model_instance_to_dict``: ``logo``
        # is an ImageField, ``model_to_dict`` hands back the FieldFile itself,
        # and a FieldFile in a JSONField raises inside ``emit_audit_event`` -
        # which swallows its own failures, so the whole event would vanish.
        before_data = {"logo": str(branding.logo) if branding and branding.logo else ""}

        SchoolBranding.objects.filter(school=school).delete()

        # Same gap as SchoolUpdateSerializer had: this read ``actor_id`` from
        # the context and never used it, so a super admin wiping a school's
        # branding left no record of it at all. Best effort and non-blocking,
        # for the reason given on the update path above.
        emit_audit_event(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.CONFIG_CHANGED,
            actor_user=self.context.get("actor_id"),
            tenant=school.tenant,
            entity_type="School",
            # Same key as the create and update paths, so a reset lands on the
            # school's one trail rather than starting a second one.
            entity_id=str(school.pk),
            entity_label=school.name,
            severity=AuditSeverity.WARNING,
            summary=f"Configuration reset for {school.name}: branding cleared",
            before_data=before_data,
            diff_data=AuditDiffService.diff_dicts(
                before_data=before_data,
                after_data={"logo": ""},
            ),
            metadata={"reason": self.validated_data.get("reason", "")},
        )

        return school


# ─────────────────────────────────────────────────────────────────────────────
# The school's own profile: what a school admin may read and edit about itself.
#
# Separate from SchoolCreate/Detail/Update above, which are the PLATFORM's view
# of a school and are gated on ``platform.schools.*``. These two are the
# school's, gated on ``school.profile.*``, and they are narrower on purpose:
# name, slug and code are allocated by CodeX when the school is created, so
# they are shown and never accepted. A school correcting its own address is a
# different decision from a school filling in its own ownership type, and only
# the second one is open here.
# ─────────────────────────────────────────────────────────────────────────────

#: Labels for the required fields, so a screen can say "ownership type" rather
#: than "ownership_type". Keyed on REQUIRED_PROFILE_FIELDS.
PROFILE_FIELD_LABELS: Dict[str, str] = {
    "name": "School name",
    "slug": "School address",
    "code": "School code",
    "ownership_type": "Ownership type",
    "term_structure": "Term structure",
    "currency": "Currency",
}


def _choice_options(choices) -> List[Dict[str, str]]:
    """A TextChoices class as ``[{value, label}]`` for a select."""
    return [{"value": value, "label": label} for value, label in choices.choices]


class SchoolProfileSerializer(serializers.ModelSerializer):
    """The school's profile as its own admin sees it.

    Carries three things the raw columns do not. ``options`` ships the choice
    vocabularies with the record, so a form cannot drift from the values the
    model will accept - the alternative is a hard-coded list in the client that
    nobody updates when a currency is added. ``missing_required`` names the
    fields still to fill, read from the model's own
    ``missing_profile_fields()``, which is the same call the onboarding gate
    makes, so the screen and the gate cannot disagree. ``editable_fields``
    states plainly which of these the school may change, so the client does not
    have to infer it from a 400.
    """

    logo = serializers.SerializerMethodField()
    missing_required = serializers.SerializerMethodField()
    options = serializers.SerializerMethodField()
    editable_fields = serializers.SerializerMethodField()

    class Meta:
        model = School
        fields = [
            # CodeX's to set, the school's to read.
            "name", "slug", "code", "status",
            # The school's own.
            "ownership_type", "term_structure", "currency",
            "address", "website", "motto", "registration_id",
            "logo",
            # Derived.
            "missing_required", "options", "editable_fields",
        ]
        read_only_fields = fields

    def get_logo(self, obj) -> str:
        branding = getattr(obj, "branding", None)
        logo = getattr(branding, "logo", None)
        if not logo:
            return ""
        request = self.context.get("request")
        return signed_url(logo.name, absolute_for=request)

    def get_missing_required(self, obj) -> List[Dict[str, str]]:
        return [
            {"field": field, "label": PROFILE_FIELD_LABELS.get(field, field)}
            for field in obj.missing_profile_fields()
        ]

    def get_options(self, obj) -> Dict[str, List[Dict[str, str]]]:
        return {
            "ownership_type": _choice_options(OwnershipType),
            "term_structure": _choice_options(TermStructure),
            "currency": _choice_options(Currency),
        }

    def get_editable_fields(self, obj) -> List[str]:
        """What this school may change, including the one that is not a field.

        ``logo`` is listed even though it is not on the update serializer: it is
        a file, so it has its own multipart endpoint. A client asking "may I
        change the logo?" wants one answer, not a note about how the transport
        differs.
        """
        return [*SchoolProfileUpdateSerializer.Meta.fields, "logo"]


class SchoolProfileUpdateSerializer(SchoolUpdateSerializer):
    """What a school may change about itself.

    Subclasses the platform's updater rather than restating it, so the audit
    event, the ``full_clean`` and the "no changes detected" refusal are the same
    code and cannot drift. The only difference is the field list.

    ``slug = None`` removes the address field the parent declares. That is the
    whole point of this class: an address correction moves the host every one of
    the school's users signs in at, and it stays a CodeX decision. ``name`` and
    ``code`` were never on the parent either, for reasons its docstring gives.

    ``branding`` is off too, and for a different reason: it holds a file. A
    nested ``ImageField`` reached through a JSON body cannot receive one, so
    leaving it here would advertise a write that silently does nothing. The logo
    has its own multipart endpoint - see ``SchoolLogoView``.
    """

    slug = None
    branding = None

    class Meta(SchoolUpdateSerializer.Meta):
        model = School
        fields = [
            "ownership_type",
            "term_structure",
            "currency",
            "address",
            "website",
            "motto",
            "registration_id",
        ]


class SchoolStaffSerializer(serializers.Serializer):
    """One person at this school, as their own school's admin sees them.

    Deliberately narrow. This renders on a screen a school admin opens during
    onboarding, so it carries what that screen shows - who they are, what they
    were invited as, and whether the invitation has landed - and none of the
    account internals the platform's own user payload carries.

    ``email`` IS here, unlike most payloads in this repo: the whole point of the
    invitations table is to show which address an invitation went to, and a
    school admin who can invite people can already see the address they typed.
    """

    id = serializers.IntegerField(read_only=True)
    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField(read_only=True)
    status = serializers.CharField(read_only=True)
    role = serializers.SerializerMethodField()
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default="",
    )
    invited_at = serializers.SerializerMethodField()
    #: True when this invitation can be sent again - which is exactly when the
    #: account is still waiting to be activated.
    can_resend = serializers.SerializerMethodField()

    def get_full_name(self, obj) -> str:
        full = (getattr(obj, "full_name", "") or "").strip()
        if full:
            return full
        return " ".join(
            part for part in (obj.first_name, obj.last_name) if part
        ).strip()

    def get_role(self, obj) -> str:
        """The roles this person holds, read from the prefetched assignments.

        Reads the prefetched list rather than querying, so a page of people
        does not become a page of queries.

        Every distinct role name, not the first one found. One person may hold
        the same role at two branches, and the prefetch is unordered, so taking
        the first match returned whichever grant the database happened to yield
        and could name a different one on the next page load. Names are
        de-duplicated so two branches of one role read as one role, and sorted
        so the same person reads the same way every time.
        """
        names = sorted({
            (assignment.role.name or assignment.role.key)
            for assignment in obj.tenant_role_assignments.all()
            if assignment.assignment_status == "ACTIVE"
            and getattr(assignment, "role", None) is not None
        })
        return ", ".join(names)

    def get_invited_at(self, obj):
        invitation = getattr(obj, "invitation", None)
        return getattr(invitation, "created_at", None) or obj.created_at

    def get_can_resend(self, obj) -> bool:
        return obj.status == "PENDING"


class SchoolBranchSerializer(serializers.ModelSerializer):
    """One branch, as a school's own branches screen reads it.

    Distinct from ``BranchListSerializer`` and ``BranchDetailSerializer``, which
    serve CodeX's branch management: those carry ``school_slug`` (a school
    already knows which school it is) and split list from detail. This is one
    shape for both, because a school's card and its detail view show the same
    facts.

    **Two of the three counts are still deliberately null.** There is no Student
    and no Teacher model in the product yet, so a number for either would be
    invented. Null says "not known" and the screen renders a dash; the day M11
    and M12 land, each becomes an annotation without the response shape changing
    under any client already reading it.

    ``classes_count`` is the first of the three to become real. M13 landed
    ``SchoolClass``, so the annotation this docstring promised is now in
    ``MyBranchListView.get_queryset``, and it counts **live** classes: an
    archived class is one the school has retired, and including it would make
    the branch card disagree with the class list beside it.

    A branch with no classes reports ``0``, which is a different claim from
    ``null`` and a true one - the school has this site and has built nothing on
    it yet.
    """

    branch_type = serializers.CharField(source="_type", read_only=True)
    students_count = serializers.SerializerMethodField()
    teachers_count = serializers.SerializerMethodField()
    classes_count = serializers.SerializerMethodField()

    def get_students_count(self, obj) -> None:
        return None

    def get_teachers_count(self, obj) -> None:
        return None

    def get_classes_count(self, obj):
        """Annotated by the view. Null only where nothing annotated it.

        The fallback matters: this serializer is reachable from the detail
        route as well as the list, and a count computed per row there would be
        a query per branch on a screen that already has its answer.
        """
        return getattr(obj, "classes_count_annotated", None)

    class Meta:
        model = Branch
        fields = [
            "id",
            "code",
            "name",
            "is_main",
            "branch_type",
            "status",
            "address",
            "email",
            "state",
            "country",
            "opened_at",
            "students_count",
            "teachers_count",
            "classes_count",
        ]
        read_only_fields = fields
