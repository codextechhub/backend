from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.forms.models import model_to_dict

logger = logging.getLogger("vs_audit")

# ---------------------------------------------------------------------------
# Summary templates — used when the caller doesn't supply a summary string.
# Keys match AuditActionType values.
# Placeholders: {actor} {entity} {entity_type}
# ---------------------------------------------------------------------------
_SUMMARY_TEMPLATES: dict[str, str] = {
    # Generic CRUD
    "CREATE":   "{actor} created {entity_type} {entity}",
    "UPDATE":   "{actor} updated {entity_type} {entity}",
    "DELETE":   "{actor} deleted {entity_type} {entity}",

    # Identity / auth
    "USER_CREATED":             "{actor} created user account for {entity}",
    "USER_INVITED":             "{actor} sent an invitation to {entity}",
    "ACCOUNT_ACTIVATED":        "{entity} activated their account",
    "LOGIN_SUCCESS":            "{entity} logged in successfully",
    "LOGIN_FAILED":             "Failed login attempt for {entity}",
    "TOKEN_REVOKED":            "Session token revoked for {entity}",
    "FORCE_LOGOUT":             "{entity} was forcefully logged out",
    "ACCOUNT_LOCKED":           "{entity}'s account was locked",
    "ACCOUNT_UNLOCKED":         "{entity}'s account was unlocked by {actor}",
    "ACCOUNT_SUSPENDED":        "{entity}'s account was suspended by {actor}",
    "ACCOUNT_REACTIVATED":      "{entity}'s account was reactivated by {actor}",
    "ACCOUNT_DEACTIVATED":      "{entity}'s account was deactivated by {actor}",
    "PASSWORD_RESET_REQUESTED": "{entity} requested a password reset",
    "PASSWORD_RESET":           "Password reset completed for {entity}",
    "PASSWORD_CHANGED":         "{entity} changed their password",
    "EMAIL_CHANGED":            "{entity}'s email address was changed",

    # Data import
    "DATA_FILE_UPLOADED":        "{actor} uploaded a data file",
    "DATA_IMPORT_STARTED":       "{actor} started a data import ({entity})",
    "DATA_IMPORT_ROW_PROCESSED": "Import row processed: {entity}",
    "DATA_IMPORT_COMPLETED":     "Data import completed: {entity}",
    "DATA_IMPORT_FAILED":        "Data import failed: {entity}",
    "DATA_IMPORT_ROLLED_BACK":   "Data import rolled back: {entity}",

    # RBAC
    "ROLE_ASSIGNED":       "{actor} assigned a role to {entity}",
    "ROLE_CHANGED":        "{actor} changed role for {entity}",
    "PERMISSION_CHANGED":  "{actor} changed permissions for {entity}",

    # Other
    "CONFIG_CHANGED":          "{actor} changed system configuration: {entity}",
    "FINANCIAL_TRANSACTION":   "Financial transaction recorded for {entity}",
    "PROCUREMENT_ACTION":      "Procurement action for {entity}",
    "EXPORT_REQUESTED":        "{actor} requested an audit log export",
    "EXPORT_COMPLETED":        "Audit log export completed",
    "EXPORT_FAILED":           "Audit log export failed",
    "CUSTOM":                  "{actor} performed an action on {entity}",
}


def _build_summary(action_type: str, actor_user, entity_label: str, entity_type: str) -> str:
    """Generate a readable one-sentence summary from available context."""
    template = _SUMMARY_TEMPLATES.get(action_type, "{actor} performed {action_type} on {entity}")

    actor = "System"
    if actor_user is not None:
        actor = (
            getattr(actor_user, "full_name", None)
            or getattr(actor_user, "get_full_name", lambda: "")()
            or getattr(actor_user, "email", None)
            or "Unknown user"
        )

    entity = entity_label or entity_type or "unknown"
    entity_type_label = entity_type or "record"

    return template.format(
        actor=actor,
        entity=entity,
        entity_type=entity_type_label,
        action_type=action_type,
    )


def emit_audit_event(
    *,
    module_key: str,
    action_type: str,
    entity_type: str,
    entity_id: str,
    actor_user=None,
    entity_label: str = "",
    severity: str = "INFO",
    status: str = "SUCCESS",
    summary: str = "",
    before_data: dict | None = None,
    diff_data: dict | None = None,
    metadata: dict | None = None,
):
    """
    Central helper: creates an AuditEvent + upserts EntityAuditTrail.

    - actor_user: pass a User instance; if None the event is attributed to SYSTEM.
    - summary: auto-generated from action_type + entity context when not provided.
    - Never raises — audit failures must never block business logic.
    - Returns the created AuditEvent, or None on failure.
    """
    from .models import AuditEvent, AuditActorType, EntityAuditTrail

    try:
        actor_type = AuditActorType.USER if actor_user is not None else AuditActorType.SYSTEM

        resolved_summary = summary or _build_summary(action_type, actor_user, entity_label, entity_type)

        event = AuditEvent.objects.create(
            module_key=module_key,
            action_type=action_type,
            actor_type=actor_type,
            actor_user=actor_user if actor_type == AuditActorType.USER else None,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_label=entity_label or "",
            severity=severity,
            status=status,
            summary=resolved_summary,
            before_data=before_data or {},
            diff_data=diff_data or {},
            metadata=metadata or {},
        )

        trail, _ = EntityAuditTrail.objects.get_or_create(
            entity_type=entity_type,
            entity_id=str(entity_id),
            defaults={"entity_label": entity_label or ""},
        )
        trail.register_event(event)

        return event

    except Exception as exc:
        logger.error("emit_audit_event failed [%s/%s entity=%s:%s]: %s", module_key, action_type, entity_type, entity_id, exc)
        return None


class AuditDiffService:
    """
    Helper service for building:
    - before_data
    - after_data
    - diff

    The goal is to produce JSON-safe audit snapshots.
    """

    @staticmethod
    def _json_safe_value(value):
        """
        Convert Python/Django values into JSON-safe values.
        """
        if isinstance(value, (datetime, date)):
            return value.isoformat()

        if isinstance(value, Decimal):
            return str(value)

        if isinstance(value, UUID):
            return str(value)

        if isinstance(value, list):
            return [AuditDiffService._json_safe_value(v) for v in value]

        if isinstance(value, dict):
            return {
                str(k): AuditDiffService._json_safe_value(v)
                for k, v in value.items()
            }

        return value

    @staticmethod
    def to_json_safe_dict(data: dict) -> dict:
        """
        Make a dictionary fully JSON-safe.
        """
        return {
            str(key): AuditDiffService._json_safe_value(value)
            for key, value in data.items()
        }

    @staticmethod
    def model_instance_to_dict(instance, *, include_fields=None, exclude_fields=None) -> dict:
        """
        Convert a Django model instance into a clean dictionary.

        Args:
            instance: Django model instance
            include_fields: optional iterable of allowed fields
            exclude_fields: optional iterable of fields to skip

        Returns:
            JSON-safe dict
        """
        if instance is None:
            return {}

        data = model_to_dict(instance)

        if include_fields:
            data = {k: v for k, v in data.items() if k in include_fields}

        if exclude_fields:
            data = {k: v for k, v in data.items() if k not in exclude_fields}

        return AuditDiffService.to_json_safe_dict(data)

    @staticmethod
    def build_after_data_from_update(
        before_data: dict,
        updates: dict,
    ) -> dict:
        """
        Build after_data by applying an update payload to before_data.

        Useful when you have:
        - old object snapshot
        - validated_data from serializer

        instead of a fully saved new instance.
        """
        merged = deepcopy(before_data)
        for key, value in updates.items():
            merged[key] = AuditDiffService._json_safe_value(value)
        return merged

    @staticmethod
    def diff_dicts(before_data: dict, after_data: dict) -> dict:
        """
        Compare two dictionaries and return only changed fields.

        Returns shape:
        {
            "field_name": {
                "before": old_value,
                "after": new_value
            }
        }
        """
        before_data = before_data or {}
        after_data = after_data or {}

        all_keys = sorted(set(before_data.keys()) | set(after_data.keys()))
        diff = {}

        for key in all_keys:
            before_value = before_data.get(key)
            after_value = after_data.get(key)

            if before_value != after_value:
                diff[key] = {
                    "before": before_value,
                    "after": after_value,
                }

        return diff

    @staticmethod
    def build_audit_snapshot(
        *,
        before_data: dict | None = None,
        after_data: dict | None = None,
    ) -> dict:
        """
        Build the full audit snapshot structure.

        Returns:
        {
            "before_data": {...},
            "after_data": {...},
            "diff": {...}
        }
        """
        before_data = AuditDiffService.to_json_safe_dict(before_data or {})
        after_data = AuditDiffService.to_json_safe_dict(after_data or {})
        diff = AuditDiffService.diff_dicts(before_data, after_data)

        return {
            "before_data": before_data,
            "after_data": after_data,
            "diff": diff,
        }

    @staticmethod
    def from_instances(
        *,
        before_instance=None,
        after_instance=None,
        include_fields=None,
        exclude_fields=None,
    ) -> dict:
        """
        Build audit snapshot from two model instances.
        """
        before_data = AuditDiffService.model_instance_to_dict(
            before_instance,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )
        after_data = AuditDiffService.model_instance_to_dict(
            after_instance,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )

        return AuditDiffService.build_audit_snapshot(
            before_data=before_data,
            after_data=after_data,
        )

    @staticmethod
    def from_instance_and_updates(
        *,
        instance,
        updates: dict,
        include_fields=None,
        exclude_fields=None,
    ) -> dict:
        """
        Build audit snapshot from:
        - existing instance
        - update payload

        Good for serializer update flows.
        """
        before_data = AuditDiffService.model_instance_to_dict(
            instance,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )
        safe_updates = AuditDiffService.to_json_safe_dict(updates or {})
        after_data = AuditDiffService.build_after_data_from_update(
            before_data=before_data,
            updates=safe_updates,
        )

        return AuditDiffService.build_audit_snapshot(
            before_data=before_data,
            after_data=after_data,
        )