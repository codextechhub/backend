"""
drf-spectacular integration for the XVS response envelope.

Every endpoint on this platform responds inside the standard envelope::

    { "success": true, "message": "...", "data": { ... } }

and list endpoints additionally carry the pagination block (see
core.pagination.XVSPagination.get_paginated_response_schema, which
drf-spectacular picks up automatically for paginated views).

The stock AutoSchema documents the *bare* serializer shape, which is not
what clients receive. EnvelopeAutoSchema wraps every 2xx JSON response in
the envelope so the generated docs match the wire format exactly.
"""
from __future__ import annotations

from drf_spectacular.openapi import AutoSchema


def _looks_enveloped(schema: dict) -> bool:
    return isinstance(schema, dict) and "success" in (schema.get("properties") or {})


def _envelope(data_schema):
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "example": True},
            "message": {"type": "string"},
            "data": data_schema,
        },
    }


# ---------------------------------------------------------------------------
# Friendly grouping + naming for the generated docs (Apidog folders/names)
# ---------------------------------------------------------------------------
# Folders: map URL prefixes to human names. Most specific match wins.
_TAG_MAP = [
    ("/v1/user/auth/",        "Authentication"),
    ("/v1/user/me/",          "My Account & Queues"),
    ("/v1/user/org",          "Organogram & Staff"),
    ("/v1/user/positions",    "Organogram & Staff"),
    ("/v1/user/position-",    "Organogram & Staff"),
    ("/v1/user/matrix-",      "Organogram & Staff"),
    ("/v1/user/staff-",       "Organogram & Staff"),
    ("/v1/user/sessions",     "Sessions & Security"),
    ("/v1/user/auth-",        "Sessions & Security"),
    ("/v1/user/lockouts",     "Sessions & Security"),
    ("/v1/user/password-resets", "Sessions & Security"),
    ("/v1/user/",             "User Management"),
    ("/v1/i/",                "Schools & Branches"),
    ("/v1/admin/tasks",       "Admin Console — Task Monitor"),
    ("/v1/admin/",            "Admin Console"),
    ("/v1/rbac/vision/",      "Permission Registry"),
    ("/v1/rbac/schools/",     "School RBAC"),
    ("/v1/rbac/platform/",    "Platform RBAC"),
    ("/v1/rbac/",             "RBAC"),
    ("/v1/audit/",            "Audit & Compliance"),
    ("/v1/config/",           "Configuration & Feature Flags"),
    ("/v1/notify/",           "Notifications"),
    ("/v1/import/",           "Data Import"),
    ("/v1/workflow/",         "Workflow Engine"),
    ("/v1/finance/",          "Finance"),
    ("/v1/procurement/",      "Procurement"),
    ("/v1/payments/",         "Payments"),
    ("/v1/todo/",             "ToDo — Org Accountability"),
    ("/media/",               "Media Files"),
]


class EnvelopeAutoSchema(AutoSchema):

    def get_tags(self):
        for prefix, tag in _TAG_MAP:
            if self.path.startswith(prefix):
                return [tag]
        return super().get_tags()

    def get_summary(self):
        """Use the first meaningful docstring line as the endpoint's name."""
        docstring = (
            getattr(self.view, "__doc__", None)
            or getattr(type(self.view), "__doc__", None)
            or ""
        )
        for line in docstring.strip().splitlines():
            line = line.strip()
            # Skip pure route lines like "POST /auth/login/" — Apidog already
            # shows the path; the sentence after it names the behaviour.
            if not line or (line.split(" ")[0].isupper() and "/" in line):
                continue
            return line.rstrip(".")[:120]
        return None

    def _get_response_for_code(self, serializer, status_code, media_types=None, direction="response"):
        response = super()._get_response_for_code(serializer, status_code, media_types, direction)
        try:
            if not str(status_code).startswith("2"):
                return response
            content = response.get("content") or {}
            for media_type, body in content.items():
                schema = body.get("schema")
                if schema is None or _looks_enveloped(schema):
                    continue
                # Paginated list schemas (XVSPagination) are already the full
                # envelope including the pagination block — leave them alone.
                props = schema.get("properties") or {}
                if "pagination" in props:
                    continue
                body["schema"] = _envelope(schema)
        except Exception:  # never break schema generation over the wrapper
            pass
        return response
