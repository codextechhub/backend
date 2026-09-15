"""Import engine fields an administrator may restrict per role.

A template's validation rules are internal configuration, read with
``import.templates.manage``, a platform-only key, and written by the template
create and update endpoints. A job's payloads and errors, and a batch's parsed
preview, are produced by the engine and never accepted from a caller. A batch's
file is the upload itself.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_PLATFORM = "PLATFORM"


def register():
    """Publish the import field declarations to the Field Access registry."""
    register_fields(
        "import",
        "templates",
        surfaces=("vs_import_data.serializers.ImportTemplateDetailSerializer",),
        fields=(
            FieldSpec("validation_rules", "Validation rules", group="Validation",
                      sensitive=True, scope=_PLATFORM, sort_order=10),
        ),
    )
    register_fields(
        "import",
        "jobs",
        surfaces=(
            "vs_import_data.serializers.ImportJobRowResultSerializer",
            "vs_import_data.serializers.ImportJobDetailSerializer",
        ),
        fields=(
            FieldSpec("row_payload", "Row as uploaded", group="Payload",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=10),
            FieldSpec("normalized_payload", "Row after cleaning", group="Payload",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=20),
            FieldSpec("execution_summary", "Execution summary", group="Payload",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=30),
            FieldSpec("error_details", "Error details", group="Errors",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=10),
            FieldSpec("last_error_code", "Last error code", group="Errors",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=20),
            FieldSpec("last_error_message", "Last error message", group="Errors",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=30),
        ),
    )
    register_fields(
        "import",
        "batches",
        surfaces=("vs_import_data.serializers.ImportBatchDetailSerializer",),
        fields=(
            FieldSpec("file", "Uploaded file", group="File", sensitive=True,
                      scope=_TENANT, sort_order=10),
            FieldSpec("preview_rows", "Preview rows", group="File", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=20),
        ),
    )
