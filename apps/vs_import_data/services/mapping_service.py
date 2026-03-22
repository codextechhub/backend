from __future__ import annotations

from django.db import transaction

from ..models import ImportColumnMapping, MappingSourceChoices


AUTO_MAPPING_RULES = {
    "students": {
        "student_name": "full_name",
        "full_name": "full_name",
        "admission_number": "admission_number",
        "email": "email",
        "class_name": "class_name",
        "gender": "gender",
        "date_of_birth": "date_of_birth",
    },
    "staff": {
        "staff_name": "full_name",
        "employee_id": "employee_id",
        "email": "email",
        "department": "department_name",
        "role": "role_name",
        "phone": "phone",
    },
    "classes": {
        "class_name": "class_name",
        "arm": "arm",
        "teacher": "class_teacher",
        "level": "level_name",
        "session": "session_name",
    },
    "fees": {
        "fee_name": "fee_name",
        "amount": "amount",
        "term": "term_name",
        "session": "session_name",
        "category": "category_name",
    },
}


def normalize_header(value: str) -> str:
    return str(value).strip().lower().replace(" ", "_")


@transaction.atomic
def apply_template_to_batch(import_batch, template):
    """
    Replace batch mappings using a saved template.
    """
    import_batch.column_mappings.all().delete()

    created = []
    mapping_schema = template.mapping_schema or {}

    for source_column, config in mapping_schema.items():
        mapping = ImportColumnMapping.objects.create(
            import_batch=import_batch,
            template=template,
            source_column=source_column,
            target_field=config.get("target_field", ""),
            source=MappingSourceChoices.TEMPLATE,
            is_required=config.get("required", False),
            is_confirmed=True,
        )
        created.append(mapping)

    template.last_used_at = import_batch.updated_at
    template.save(update_fields=["last_used_at"])

    return created


@transaction.atomic
def auto_map_columns(import_batch, overwrite_existing: bool = False):
    """
    Create mappings automatically based on simple header rules.
    """
    if overwrite_existing:
        import_batch.column_mappings.all().delete()

    existing_source_columns = set(
        import_batch.column_mappings.values_list("source_column", flat=True)
    )

    rules = AUTO_MAPPING_RULES.get(import_batch.dataset_type, {})
    detected_columns = import_batch.detected_columns or []

    created = []

    for raw_header in detected_columns:
        normalized = normalize_header(raw_header)

        if not overwrite_existing and raw_header in existing_source_columns:
            continue

        target_field = rules.get(normalized)
        if not target_field:
            continue

        mapping = ImportColumnMapping.objects.create(
            import_batch=import_batch,
            source_column=raw_header,
            target_field=target_field,
            source=MappingSourceChoices.AUTO,
            confidence_score=95.00,
            is_required=False,
            is_confirmed=False,
        )
        created.append(mapping)

    return created


@transaction.atomic
def save_bulk_mappings(import_batch, mappings: list[dict], clear_existing: bool = False):
    """
    Save many mappings at once.
    """
    if clear_existing:
        import_batch.column_mappings.all().delete()

    created = []

    for item in mappings:
        mapping = ImportColumnMapping.objects.create(
            import_batch=import_batch,
            source_column=item["source_column"],
            target_field=item["target_field"],
            source=item.get("source", "manual"),
            confidence_score=item.get("confidence_score"),
            is_required=item.get("is_required", False),
            is_confirmed=item.get("is_confirmed", True),
        )
        created.append(mapping)

    return created