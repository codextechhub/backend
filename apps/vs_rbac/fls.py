"""
Field Level Security (FLS) mixin for DRF serializers.

Protects individual fields behind permission keys so the backend
strips unreadable fields from responses and rejects unauthorized
writes — regardless of what the frontend renders.

Usage
-----
    from vs_rbac.fls import FieldSecurityMixin

    class StudentSerializer(FieldSecurityMixin, serializers.ModelSerializer):
        read_permissions = {
            "medical_notes":        "students.medical.view",
            "disciplinary_notes":   "students.disciplinary.view",
            "guardian_contacts":    "students.guardian.view",
        }
        write_permissions = {
            "enrolment_date":       "students.enrol",
            "medical_notes":        "students.medical.manage",
        }
        class Meta:
            model = Student
            fields = "__all__"

Behaviour
---------
* Fields absent from both dicts are always exposed — FLS is opt-in per field.
* When the serializer is called without a request context (management commands,
  login payload construction, tests that bypass auth) all fields pass through
  unchanged, so nothing silently disappears.
* Permissions are resolved once per request and cached on ``request._fls_permissions``
  so a list endpoint that serializes 200 student records only hits the DB once.
* On writes, every unauthorized field raises a per-field ValidationError so the
  caller knows exactly which fields were rejected.
"""
from __future__ import annotations

from typing import Any

from rest_framework import serializers


class FieldSecurityMixin:
    """
    Mixin for ``serializers.Serializer`` / ``serializers.ModelSerializer``.

    Declare class attributes:
        read_permissions  : dict[str, str]  – {field_name: permission_key}
        write_permissions : dict[str, str]  – {field_name: permission_key}

    Both default to empty dicts (no-op) if omitted.
    """

    read_permissions: dict[str, str] = {}
    write_permissions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_user_permissions(self) -> set[str] | None:
        """
        Return the user's effective permission set for this request.

        Returns ``None`` when there is no usable request context, which signals
        callers to skip FLS entirely (allow all fields).
        """
        request = self.context.get("request")  # type: ignore[attr-defined]
        if not request:
            return None

        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return set()

        # Vision super admins bypass FLS entirely — they get all fields regardless of grants.
        from vs_rbac.permissions import is_vision_super_admin
        if is_vision_super_admin(user):
            return None

        # Cache for the lifetime of the request so serializing a list of 1 000
        # records does not fire 1 000 separate evaluator queries.
        if not hasattr(request, "_fls_permissions"):
            from vs_rbac.evaluator import get_effective_permissions
            school = getattr(user, "school", None)
            request._fls_permissions = get_effective_permissions(user, school=school)

        return request._fls_permissions  # type: ignore[return-value]

    def _can_read(self, field: str, user_perms: set[str]) -> bool:
        perm = self.read_permissions.get(field)
        return perm is None or perm in user_perms

    def _can_write(self, field: str, user_perms: set[str]) -> bool:
        perm = self.write_permissions.get(field)
        return perm is None or perm in user_perms

    # ------------------------------------------------------------------
    # DRF hooks
    # ------------------------------------------------------------------

    def to_representation(self, instance: Any) -> dict:
        data = super().to_representation(instance)  # type: ignore[misc]

        if not self.read_permissions:
            return data

        user_perms = self._resolve_user_permissions()
        if user_perms is None:
            return data  # no request context — skip FLS

        stripped: list[str] = []
        for field in list(data.keys()):
            if not self._can_read(field, user_perms):
                data.pop(field)
                stripped.append(field)

        if stripped:
            data["_stripped_fields"] = stripped

        return data

    def to_internal_value(self, data: Any) -> dict:
        if self.write_permissions:
            user_perms = self._resolve_user_permissions()
            if user_perms is not None:
                errors = {
                    field: "You do not have permission to modify this field."
                    for field in self.write_permissions
                    if field in data and not self._can_write(field, user_perms)
                }
                if errors:
                    raise serializers.ValidationError(errors)

        return super().to_internal_value(data)  # type: ignore[misc]
