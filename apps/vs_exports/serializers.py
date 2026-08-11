"""Serializers for the Export Centre.

Two things to keep in mind when editing:

**Nothing here exposes a raw JSONField.** ``frozen_config``, ``format_options`` and
``filters`` are all JSON on the model, but each is re-shaped for the API - the run
serializer publishes the readable summary, not the blob - so an internal id or a stray
key added later cannot leak by default.

**Write serializers validate against the catalogue, not against themselves.** Columns,
filters and format options are only meaningful relative to a dataset, so
:class:`ExportDefinitionWriteSerializer` resolves the dataset first and rejects
anything the dataset does not declare. That is also where mass assignment is stopped:
``owner``, ``tenant`` and ``entity`` are never read from the payload.
"""
from __future__ import annotations

from rest_framework import serializers

from .catalogue import describe_filter, get_dataset
from .constants import (
    DatasetScope,
    ExportFormat,
    FAILURE_GUIDANCE,
    RETRYABLE_FAILURE_CODES,
    ValuesMode,
)
from .models import (
    ExportDefinition,
    ExportDownload,
    ExportFile,
    ExportRun,
)


# --------------------------------------------------------------------------- #
# Definitions                                                                 #
# --------------------------------------------------------------------------- #
class ExportDefinitionListSerializer(serializers.ModelSerializer):
    """Row shape for the Saved exports table."""

    dataset = serializers.SerializerMethodField()
    # Not source="entity.code": a tenant-scoped export has no entity, and DRF would
    # raise walking through the null rather than returning one.
    entity_code = serializers.SerializerMethodField()
    scope = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()
    shared_with = serializers.SerializerMethodField()
    last_run = serializers.SerializerMethodField()
    column_count = serializers.SerializerMethodField()

    class Meta:
        model = ExportDefinition
        fields = [
            "id", "name", "description", "dataset", "scope", "entity_code", "format",
            "values_mode", "column_count", "owner_name", "is_owner", "sharing",
            "shared_with", "is_draft", "is_archived", "last_run", "updated_at",
        ]

    def get_dataset(self, obj):
        dataset = obj.dataset
        return {
            "id": obj.dataset_key,
            "name": dataset.name if dataset else obj.dataset_key,
            "module": dataset.module if dataset else "",
            # A withdrawn dataset is stated, not hidden - the row must explain itself.
            "available": dataset is not None,
        }

    def get_entity_code(self, obj):
        return obj.entity.code if obj.entity_id else None

    def get_scope(self, obj):
        """What this export covers, in the words the list column uses."""
        if obj.entity_id:
            return {"type": DatasetScope.ENTITY, "label": obj.entity.code}
        return {"type": DatasetScope.TENANT, "label": obj.tenant.name}

    def get_owner_name(self, obj):
        owner = obj.owner
        return getattr(owner, "get_full_name", lambda: "")() or getattr(owner, "email", "")

    def get_is_owner(self, obj):
        user = getattr(self.context.get("request"), "user", None)
        return bool(user and obj.owner_id == user.pk)

    def get_shared_with(self, obj):
        return obj.shares.count()

    def get_column_count(self, obj):
        return len(obj.columns or [])

    def get_last_run(self, obj):
        run = obj.runs.order_by("-queued_at").first()
        if run is None:
            return None
        return {"reference": run.reference, "status": run.status, "at": run.queued_at}


class ExportDefinitionDetailSerializer(ExportDefinitionListSerializer):
    """Everything the builder needs to reopen an export."""

    filters_readable = serializers.SerializerMethodField()

    class Meta(ExportDefinitionListSerializer.Meta):
        fields = ExportDefinitionListSerializer.Meta.fields + [
            "columns", "filters", "filters_readable", "sort", "format_options",
            "file_name_pattern", "created_at",
        ]

    def get_filters_readable(self, obj):
        dataset = obj.dataset
        if dataset is None:
            return []
        return [describe_filter(dataset, spec) for spec in obj.filters or []]


class ExportDefinitionWriteSerializer(serializers.ModelSerializer):
    """Create/update payload, validated against the catalogue.

    ``tenant``, ``entity`` and ``owner`` are set by the view from the request - never
    from the body - so a caller cannot assign an export to another tenant, another
    entity or another person by adding a field.
    """

    dataset_key = serializers.CharField()

    class Meta:
        model = ExportDefinition
        fields = [
            "name", "description", "dataset_key", "columns", "filters", "sort",
            "format", "format_options", "values_mode", "file_name_pattern",
            "sharing", "is_draft",
        ]

    def validate_dataset_key(self, value):
        if get_dataset(value) is None:
            raise serializers.ValidationError(
                f"'{value}' is not a dataset your administrators publish."
            )
        return value

    def validate(self, attrs):
        # On a partial update the dataset may not be in the payload; fall back to the
        # instance, because columns and filters can only be judged against a dataset.
        key = attrs.get("dataset_key") or getattr(self.instance, "dataset_key", "")
        dataset = get_dataset(key)
        if dataset is None:
            raise serializers.ValidationError(
                {"dataset_key": "This export's dataset is no longer published."}
            )

        fmt = attrs.get("format") or getattr(self.instance, "format", ExportFormat.XLSX)
        if fmt not in [str(f) for f in dataset.formats]:
            raise serializers.ValidationError({
                "format": f"{dataset.name} cannot be produced as {fmt}.",
            })

        columns = attrs.get("columns", getattr(self.instance, "columns", None)) or []
        unknown = [c for c in columns if dataset.field(c) is None]
        if unknown:
            raise serializers.ValidationError({
                "columns": f"{', '.join(unknown)} are not fields on {dataset.name}.",
            })

        filters = attrs.get("filters", getattr(self.instance, "filters", None)) or []
        for spec in filters:
            if not isinstance(spec, dict) or not spec.get("id"):
                raise serializers.ValidationError({
                    "filters": "Every filter needs an 'id' naming the filter it sets.",
                })
            if dataset.filter_def(str(spec["id"])) is None:
                raise serializers.ValidationError({
                    "filters": f"'{spec['id']}' is not a filter on {dataset.name}.",
                })

        # A draft may be incomplete; anything runnable must be complete.
        is_draft = attrs.get("is_draft", getattr(self.instance, "is_draft", False))
        if not is_draft:
            if not columns:
                raise serializers.ValidationError({
                    "columns": "Choose at least one column before saving this export.",
                })
            present = {str(f.get("id")) for f in filters}
            missing = [
                dataset.filter_def(fid).label
                for fid in dataset.required_filter_ids if fid not in present
            ]
            if missing:
                raise serializers.ValidationError({
                    "filters": (
                        f"{dataset.name} needs {', '.join(missing)} to be set. Save it as "
                        f"a draft if you are not ready."
                    ),
                })

        # Keep the options object discriminated by format.
        from .services import clean_format_options

        attrs["format_options"] = clean_format_options(fmt, attrs.get("format_options"))
        return attrs


# --------------------------------------------------------------------------- #
# Runs and files                                                              #
# --------------------------------------------------------------------------- #
class ExportFileSerializer(serializers.ModelSerializer):
    """File card shape. Availability is derived here, never stored."""

    is_expired = serializers.BooleanField(read_only=True)
    is_purged = serializers.BooleanField(read_only=True)
    is_downloadable = serializers.BooleanField(read_only=True)

    class Meta:
        model = ExportFile
        fields = [
            "id", "name", "format", "size_bytes", "row_count", "columns_produced",
            "available_until", "purged_at", "download_count", "is_expired",
            "is_purged", "is_downloadable",
        ]


class ExportRunListSerializer(serializers.ModelSerializer):
    """Row shape for the Files list."""

    export_name = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    # Null for a quick export. The UI needs it to offer "Edit export" on the
    # failures whose only real fix is changing the recipe; the definition is
    # already visible to this caller under the same visibility rule.
    definition_id = serializers.IntegerField(read_only=True)
    file = ExportFileSerializer(read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = ExportRun
        fields = [
            "id", "reference", "export_name", "definition_id", "status", "trigger",
            "requested_by_name", "queued_at", "started_at", "ended_at", "row_count",
            "attempt", "progress", "file",
        ]

    def get_export_name(self, obj):
        return (obj.frozen_config or {}).get("name") or "Quick export"

    def get_requested_by_name(self, obj):
        user = obj.requested_by
        return getattr(user, "get_full_name", lambda: "")() or getattr(user, "email", "")

    def get_progress(self, obj):
        """``{phase, rows_done, rows_total, queue_position}``.

        A null ``rows_total`` means the UI renders indeterminate progress - expected,
        not an error. ``queue_position`` is null once the run starts; while it is
        queued it is what lets the UI explain a wait instead of going quiet.
        """
        if obj.is_terminal:
            return None
        from .services import queue_position

        return {
            "phase": obj.phase,
            "phase_label": obj.get_phase_display(),
            "rows_done": obj.rows_done,
            "rows_total": obj.rows_total,
            "queue_position": queue_position(obj),
        }


class ExportRunDetailSerializer(ExportRunListSerializer):
    """Run detail: the outcome, what produced it, and how it has drifted since."""

    failure = serializers.SerializerMethodField()
    configuration = serializers.SerializerMethodField()
    drift = serializers.SerializerMethodField()

    class Meta(ExportRunListSerializer.Meta):
        fields = ExportRunListSerializer.Meta.fields + [
            "omissions", "failure", "configuration", "drift",
        ]

    def get_failure(self, obj):
        """The outcome as a person can act on it: what happened, and what to do.

        ``retryable`` is deliberately narrow. Only a transient infrastructure
        fault gets a Retry button - a filter, permission, row-cap or date-span
        failure fails again in exactly the same way, so offering a retry there
        wastes the user's time and teaches them the button does not work. For
        those, ``recommended_action`` is the real next step and the UI leads
        with it instead.
        """
        if not obj.failure_code:
            return None
        return {
            "code": obj.failure_code,
            "message": obj.failure_message,
            "recommended_action": FAILURE_GUIDANCE.get(obj.failure_code, ""),
            "reference": obj.reference,
            "retryable": (
                obj.definition_id is not None
                and obj.failure_code in RETRYABLE_FAILURE_CODES
            ),
        }

    def get_configuration(self, obj):
        """The frozen config as labels, not as the stored blob."""
        config = obj.frozen_config or {}
        dataset = get_dataset(config.get("dataset_key"))
        columns = [
            (dataset.field(c).label if dataset and dataset.field(c) else c)
            for c in config.get("columns") or []
        ]
        filters = (
            [describe_filter(dataset, spec) for spec in config.get("filters") or []]
            if dataset else []
        )
        return {
            "dataset": dataset.name if dataset else config.get("dataset_key"),
            "scope": config.get("entity_code") or "Whole organisation",
            "columns": columns,
            "filters": filters,
            "format": config.get("format"),
            "values_mode": config.get("values_mode"),
        }

    def get_drift(self, obj):
        """How the definition has moved on since this run - as READABLE changes.

        `config_drift` returns the raw before/after, which is the stored blob:
        column ids, filter specs, format-option dicts. Publishing that would put
        a raw JSONField on the wire, which this module does not do. So each
        change is rendered through the same helpers the configuration block uses
        - labels, filter sentences, format names - and the UI can show "what
        changed" without ever seeing an internal id.
        """
        from .services import config_drift

        dataset = get_dataset((obj.frozen_config or {}).get("dataset_key"))
        changes = [
            {
                "field": c["field"],
                "label": _DRIFT_LABELS.get(c["field"], c["field"].replace("_", " ").capitalize()),
                "then": _readable_config_value(c["field"], c["then"], dataset),
                "now": _readable_config_value(c["field"], c["now"], dataset),
            }
            for c in config_drift(obj)
        ]
        return {
            "count": len(changes),
            "fields": [c["field"] for c in changes],
            "changes": changes,
        }



# What each drifted key is called on screen.
_DRIFT_LABELS = {
    "dataset_key": "Dataset",
    "columns": "Columns",
    "filters": "Filters",
    "sort": "Sorting",
    "format": "Format",
    "format_options": "Format options",
    "values_mode": "Values",
    "file_name_pattern": "File name",
    "name": "Name",
}


# Render one side of a drift entry as a sentence, never as the stored blob.
def _readable_config_value(field, value, dataset) -> str:
    """A person-readable rendering of one configuration value.

    Deliberately lossy: the point is "what changed", not a reconstructable dump.
    Column ids become labels, filter specs become the sentences the review step
    already shows, and an options object becomes a count rather than its keys.
    """
    if value in (None, "", [], {}):
        return "-"
    if field == "columns":
        labels = [
            (dataset.field(c).label if dataset and dataset.field(c) else str(c))
            for c in value
        ]
        return ", ".join(labels) or "-"
    if field == "filters":
        if not dataset:
            return f"{len(value)} filters"
        return "; ".join(describe_filter(dataset, spec) for spec in value) or "-"
    if field == "sort":
        return ", ".join(
            f"{s.get('field')} {s.get('direction', 'asc')}" for s in value
        ) or "-"
    if field == "format_options":
        return f"{len(value)} option{'' if len(value) == 1 else 's'} set"
    if field == "dataset_key":
        other = get_dataset(str(value))
        return other.name if other else str(value)
    if field == "values_mode":
        return "For people to read" if value == ValuesMode.PEOPLE else "For another system"
    if field == "format":
        return str(value).upper()
    return str(value)


class ExportDownloadSerializer(serializers.ModelSerializer):
    """The download log - who took it, when, and who was refused."""

    user_name = serializers.SerializerMethodField()

    class Meta:
        model = ExportDownload
        fields = ["id", "user_name", "at", "ip_address", "outcome", "refusal_reason"]

    def get_user_name(self, obj):
        user = obj.user
        return getattr(user, "get_full_name", lambda: "")() or getattr(user, "email", "-")


# --------------------------------------------------------------------------- #
# Ad-hoc payloads                                                             #
# --------------------------------------------------------------------------- #
class PreviewSerializer(serializers.Serializer):
    """Body for the preview/estimate endpoint - a configuration, not a saved export."""

    dataset_key = serializers.CharField()
    columns = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    filters = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    sort = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    format = serializers.ChoiceField(choices=ExportFormat.choices, default=ExportFormat.XLSX)
    values_mode = serializers.ChoiceField(choices=ValuesMode.choices, default=ValuesMode.PEOPLE)

    def validate_dataset_key(self, value):
        if get_dataset(value) is None:
            raise serializers.ValidationError(
                f"'{value}' is not a dataset your administrators publish."
            )
        return value


class QuickExportSerializer(PreviewSerializer):
    """Body for a run that was never saved as a definition.

    Same shape the preview endpoint validates, so the drawer can send exactly what it
    just previewed, plus a display name and the idempotency key.
    """

    name = serializers.CharField(required=False, allow_blank=True, max_length=180)
    format_options = serializers.DictField(required=False, default=dict)
    client_key = serializers.CharField(
        required=False, allow_blank=True, max_length=64,
    )


class RunRequestSerializer(serializers.Serializer):
    """Body for triggering a run."""

    client_key = serializers.CharField(
        required=False, allow_blank=True, max_length=64,
        help_text="Idempotency key - the same key inside 60s returns the run already going.",
    )


class ShareSerializer(serializers.Serializer):
    """Body for sharing a definition with people."""

    user_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=True)


class AnalyticsIngestSerializer(serializers.Serializer):
    """A batch of client-reported analytics events.

    The browser reports what only it knows - which wizard step someone was on when
    they gave up, how long they lingered. Validation is deliberately narrow: an unknown
    event name is rejected here, and :func:`vs_exports.analytics.sanitise` drops any
    property the event does not declare, so this endpoint cannot become a channel for
    arbitrary data.
    """

    events = serializers.ListField(
        child=serializers.DictField(), allow_empty=False, max_length=50,
    )
    session_key = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def validate_events(self, value):
        from .analytics import CLIENT_EVENTS

        for item in value:
            name = str(item.get("name") or "")
            if name not in CLIENT_EVENTS:
                raise serializers.ValidationError(
                    f"'{name}' is not an event the client may report."
                )
        return value
