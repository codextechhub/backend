from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import serializers

from vs_tenants.models import Tenant
from vs_tenants.references import TENANT_NOT_FOUND
from .models import (
    AuditEvent,
    EntityAuditTrail,
    AuditExportJob,
    ComplianceRule,
    AuditSeverity,
    AuditActorType,
    AuditStatus,
    AuditModuleKey,
    AuditActionType,
    ExportFormat,
    ExportJobStatus,
    ComplianceRuleType,
)

User = get_user_model()


# -----------------------------------------------------------------------------
# Small reusable serializers
# -----------------------------------------------------------------------------

class TenantSlimSerializer(serializers.ModelSerializer):
    """
    Small tenant serializer.
    Use this when you only want basic tenant details inside another response.
    """

    class Meta:
        model = Tenant
        fields = ("id", "name", "slug")


class UserSlimSerializer(serializers.ModelSerializer):
    """
    Small user serializer.
    Adjust fields if your User model uses different names.
    """
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "full_name")


# -----------------------------------------------------------------------------
# Audit Event Serializers
# -----------------------------------------------------------------------------

class AuditEventListSerializer(serializers.ModelSerializer):
    """
    Use this for audit log listing pages.

    Why it exists:
    - list pages usually need lighter data
    - we avoid dumping all JSON snapshots on every row
    """

    actor_user = UserSlimSerializer(read_only=True)
    effective_user = UserSlimSerializer(read_only=True)
    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    ip_address = serializers.SerializerMethodField()
    entity_user = serializers.SerializerMethodField()

    def get_ip_address(self, obj):
        return (obj.metadata or {}).get("ip_address")

    def get_entity_user(self, obj):
        if obj.entity_type != "User" or not obj.entity_id:
            return None
        users_map = self.context.get("entity_users", {})
        user = users_map.get(str(obj.entity_id))
        if not user:
            return None
        return {"id": str(user.id), "full_name": user.full_name, "email": user.email}

    class Meta:
        model = AuditEvent
        fields = (
            "id",
            "module_key",
            "action_type",
            "severity",
            "status",
            "actor_type",
            "actor_user",
            "effective_user",
            "tenant",
            "impersonation_session",
            "actor_label",
            "entity_type",
            "entity_id",
            "entity_label",
            "entity_user",
            "summary",
            "ip_address",
            "event_at",
        )


class AuditEventDetailSerializer(serializers.ModelSerializer):
    """
    Use this for opening a single audit event in detail view.

    This includes snapshots and metadata.
    """

    actor_user = UserSlimSerializer(read_only=True)
    effective_user = UserSlimSerializer(read_only=True)
    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = AuditEvent
        fields = (
            "id",
            "module_key",
            "action_type",
            "severity",
            "status",
            "actor_type",
            "actor_user",
            "effective_user",
            "tenant",
            "impersonation_session",
            "actor_label",
            "entity_type",
            "entity_id",
            "entity_label",
            "summary",
            "before_data",
            "diff_data",
            "metadata",
            "event_at",
        )


# -----------------------------------------------------------------------------
# Entity Audit Trail Serializers
# -----------------------------------------------------------------------------

# The trail's two timestamps are computed rather than model fields, so
# borrowing a real DateTimeField is what renders them like every other
# datetime the API emits, including under a project DATETIME_FORMAT.
_TRAIL_DATETIME = serializers.DateTimeField()


def _isoformat(value):
    """Render a computed datetime the way DRF renders a stored one."""
    return _TRAIL_DATETIME.to_representation(value) if value else None


class EntityAuditTrailSerializer(serializers.ModelSerializer):
    """The catalogue row: which entity, what it is called, how big its trail is.

    Only the first two come off the model. ``EntityAuditTrail`` stores no
    counters - it stopped being a rollup because the rollup only ever grew (see
    the model docstring) - so ``event_count``, ``first_event_at`` and
    ``last_event_at`` are read out of ``visible_counters``, which the view puts
    in the context and :func:`vs_audit.scoping.visible_trail_counters` fills
    from ``AuditEvent`` in one grouped query per page.

    They are declared read-only method fields rather than model fields for a
    reason worth keeping: there is no longer any column for a write to land in,
    so no code path can persist a count at all, whether it means to or not.

    A pair missing from ``visible_counters`` reports ``0``/``null``: the caller
    can read no event on that entity, or the entity has none left. That is the
    honest answer for a trail whose events were deleted underneath it, and it is
    the answer the old stored figure could not give.
    """

    event_count = serializers.SerializerMethodField()
    first_event_at = serializers.SerializerMethodField()
    last_event_at = serializers.SerializerMethodField()

    class Meta:
        model = EntityAuditTrail
        fields = (
            "id",
            "entity_type",
            "entity_id",
            "entity_label",
            "event_count",
            "first_event_at",
            "last_event_at",
        )

    def _counters(self, instance):
        """The caller's counters for one trail, or an empty dict meaning zero.

        A view that forgets the context gets zeros, which is visibly wrong on
        the screen and discloses nothing - the safe direction for a default to
        fail in. Both views supply it; ``EntityAuditTrailDetailSerializer``
        instantiates this class for schema generation only and never serializes
        through it.
        """
        counters = self.context.get("visible_counters") or {}
        return counters.get((instance.entity_type, instance.entity_id)) or {}

    def get_event_count(self, instance) -> int:
        return self._counters(instance).get("event_count", 0)

    def get_first_event_at(self, instance) -> str | None:
        return _isoformat(self._counters(instance).get("first_event_at"))

    def get_last_event_at(self, instance) -> str | None:
        return _isoformat(self._counters(instance).get("last_event_at"))


class EntityAuditTrailDetailSerializer(serializers.Serializer):
    """
    This is NOT tied directly to one model row.

    Why?
    Because a full entity trail page often needs:
    - the trail summary
    - the actual list of event rows

    So this serializer groups both together.
    """

    trail = EntityAuditTrailSerializer()
    events = AuditEventListSerializer(many=True)


# -----------------------------------------------------------------------------
# Audit Export Job Serializers
# -----------------------------------------------------------------------------

class _ExportDownloadUrlMixin(serializers.Serializer):
    """Publish the authorised download path, never the storage key.

    ``AuditExportJob.file_path`` holds the key ``core.storage`` chose. Exposing
    it would put the audit trail's location into every payload that lists a job,
    and the key would go on naming the file long after the job expired. What
    these serializers expose instead is the route that re-asks the permission
    question, checks expiry, and confirms the job is the caller's own, on every
    single call.
    """

    download_url = serializers.SerializerMethodField()

    def get_download_url(self, obj):
        """Return the authorised download path, or None when there is nothing to take."""
        if obj.status != ExportJobStatus.COMPLETED or not obj.file_path or obj.is_expired:
            return None
        return reverse("audit-export-download", kwargs={"id": obj.id})


class AuditExportJobListSerializer(_ExportDownloadUrlMixin, serializers.ModelSerializer):
    """
    Lighter serializer for export history listing.
    """

    requested_by = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditExportJob
        fields = (
            "id",
            "requested_by",
            "export_format",
            "status",
            "file_name",
            "download_url",
            "row_count",
            "requested_at",
            "started_at",
            "completed_at",
            "expires_at",
        )


class AuditExportJobDetailSerializer(_ExportDownloadUrlMixin, serializers.ModelSerializer):
    """
    Full serializer for one export job.
    """

    requested_by = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditExportJob
        fields = (
            "id",
            "requested_by",
            "export_format",
            "status",
            "filter_payload",
            "file_name",
            "download_url",
            "row_count",
            "failure_reason",
            "requested_at",
            "started_at",
            "completed_at",
            "expires_at",
        )


# -----------------------------------------------------------------------------
# Compliance Rule Serializers
# -----------------------------------------------------------------------------

class ComplianceRuleListSerializer(serializers.ModelSerializer):
    """
    Lighter serializer for listing compliance rules.
    """

    tenant = TenantSlimSerializer(read_only=True)

    class Meta:
        model = ComplianceRule
        fields = (
            "id",
            "name",
            "description",
            "rule_type",
            "tenant",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
            "updated_at",
        )


class ComplianceRuleDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one compliance rule.
    """

    tenant = TenantSlimSerializer(read_only=True)

    class Meta:
        model = ComplianceRule
        fields = (
            "id",
            "name",
            "description",
            "rule_type",
            "tenant",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
            "masking_fields",
            "config",
            "created_at",
            "updated_at",
        )


class ComplianceRuleCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Use this for creating and updating compliance rules.
    """

    class Meta:
        model = ComplianceRule
        fields = (
            "name",
            "description",
            "rule_type",
            "tenant",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
            "masking_fields",
            "config",
        )

    def validate(self, attrs):
        rule_type = attrs.get("rule_type")
        retention_days = attrs.get("retention_days")

        if rule_type == ComplianceRuleType.RETENTION and not retention_days:
            raise serializers.ValidationError(
                {"retention_days": "Retention rules require retention_days."}
            )

        return attrs


# -----------------------------------------------------------------------------
# Search / Filter Serializer
# -----------------------------------------------------------------------------

class ChoiceListField(serializers.ListField):
    """Accept one choice or repeated/list choices and always return a list.

    Query parameters commonly arrive as ``?status=FAILED&status=DENIED`` while
    older callers still send a single scalar.  Keeping that compatibility at
    the validation boundary gives the event list and CSV export one contract.
    """

    def __init__(self, *, choices, **kwargs):
        super().__init__(child=serializers.ChoiceField(choices=choices), **kwargs)

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = [data]
        return super().to_internal_value(data)


#: What ``tenant_slug`` accepts to mean "events that belong to no customer" -
#: platform operations, sweeps and management commands. Without it the filter
#: could only ever narrow *to* a tenant and the platform-layer rows would be
#: unreachable from the Explorer entirely.
#:
#: It is spelled with underscores because ``tenant_slug_validator`` forbids
#: them: no school can ever register a slug that collides with this token, so
#: the sentinel cannot be shadowed by a customer called "None Academy".
NO_TENANT = "__none__"


class AuditEventFilterSerializer(serializers.Serializer):
    """Validate Event Explorer query parameters; nothing here is saved.

    The customer filter is named ``tenant_slug`` rather than ``tenant``.
    ``tenant`` is reserved platform-wide for the tenant assertion: every
    authenticated request carries ``?tenant=<slug>`` and
    ``TenantJWTAuthentication`` reads it before this serializer sees the query
    string, so a filter of that name would re-scope the whole request instead
    of narrowing it. A CX staffer typing ``?tenant=bright-star`` to narrow the
    Explorer would be answered "No tenant matches the requested context".

    It takes the slug rather than the primary key, unlike ``actor_user_id``
    beside it, because the slug is the identifier ``Tenant`` publishes and the
    value ``AuditEventListSerializer`` already renders in the ``tenant``
    column. The console filters on exactly what it displays.
    """

    module_key = ChoiceListField(
        choices=AuditModuleKey.choices,
        required=False,
    )
    action_type = ChoiceListField(
        choices=AuditActionType.choices,
        required=False,
    )
    severity = ChoiceListField(
        choices=AuditSeverity.choices,
        required=False,
    )
    status = ChoiceListField(
        choices=AuditStatus.choices,
        required=False,
    )
    actor_type = serializers.ChoiceField(
        choices=AuditActorType.choices,
        required=False,
    )
    actor_user_id = serializers.IntegerField(required=False)
    impersonation_session_id = serializers.IntegerField(required=False)
    entity_type = serializers.CharField(required=False)
    entity_id = serializers.CharField(required=False)

    # Narrow to one customer. See the class docstring.
    tenant_slug = serializers.CharField(required=False)

    date_from = serializers.DateTimeField(required=False)
    date_to = serializers.DateTimeField(required=False)

    search = serializers.CharField(required=False, allow_blank=True)

    def validate_tenant_slug(self, value):
        """Reject a slug no tenant answers to, rather than returning nothing.

        The other validated filters on this serializer refuse a value outside
        their vocabulary - a misspelt ``module_key`` is a 400, not an empty
        list - and the vocabulary here is "the tenants that exist". Silently
        answering an unknown slug with zero rows would recreate the exact trap
        this filter was built to remove: Chidera types ``bright-star-school``
        for a school registered as ``bright-star``, gets an empty Explorer, and
        reports that nothing happened there.
        """
        value = (value or "").strip().lower()
        if value == NO_TENANT:
            return value
        if not Tenant.objects.filter(slug=value).exists():
            raise serializers.ValidationError(TENANT_NOT_FOUND)
        return value

    def validate(self, attrs):
        """
        Validate date range.
        """

        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")

        if date_from and date_to and date_from > date_to:
            raise serializers.ValidationError(
                {"date_to": "date_to must be greater than or equal to date_from."}
            )

        for field in ("entity_type", "entity_id", "search"):
            if field in attrs:
                attrs[field] = attrs[field].strip()

        return attrs
