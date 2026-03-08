# backend/apps/audit_logging/services.py

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.forms.models import model_to_dict


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