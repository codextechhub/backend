from __future__ import annotations

from ..models import ImportTemplate, TemplateStatusChoices


def get_active_template_by_dataset(dataset_type: str) -> ImportTemplate:
    """
    Return the current active system template for a dataset type.
    """
    return ImportTemplate.objects.prefetch_related("columns").get(
        dataset_type=dataset_type,
        status=TemplateStatusChoices.ACTIVE,
        is_download_enabled=True,
    )


def get_template_headers(template: ImportTemplate) -> list[str]:
    """
    Return ordered expected headers from template columns.
    """
    return list(
        template.columns.order_by("column_order").values_list("column_name", flat=True)
    )