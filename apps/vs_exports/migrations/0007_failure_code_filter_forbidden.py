"""A run can fail with ``FILTER_FORBIDDEN``: a filter reads a field its owner cannot see.

Choices only; no column or row changes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_exports", "0006_money_filters_in_naira"),
    ]

    operations = [
        migrations.AlterField(
            model_name="exportrun",
            name="failure_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("DATASET_WITHDRAWN", "The dataset is no longer published"),
                    ("DATASET_FORBIDDEN", "No longer allowed to export this dataset"),
                    ("ENTITY_FORBIDDEN", "No longer allowed to use this entity"),
                    ("FILTER_INVALID", "A filter this export uses no longer exists"),
                    (
                        "FILTER_FORBIDDEN",
                        "A filter this export uses reads a field you cannot see",
                    ),
                    ("REQUIRED_FILTER_MISSING", "A required filter is not set"),
                    ("NO_COLUMNS", "Every selected column has become unavailable"),
                    ("ROW_CAP_EXCEEDED", "The result is larger than the row cap"),
                    (
                        "DATE_SPAN_EXCEEDED",
                        "The date range is wider than the dataset allows",
                    ),
                    ("OWNER_INACTIVE", "The owner of this export is no longer active"),
                    ("INFRASTRUCTURE", "A temporary system problem"),
                    ("UNKNOWN", "An unexpected problem"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
