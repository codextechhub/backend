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


class EnvelopeAutoSchema(AutoSchema):

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
