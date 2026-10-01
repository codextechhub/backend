"""Store each staff member's leave group and each request's planned return.

The group and exception catalogues are school settings. Existing staff keep
the school-wide allowance, and existing leave requests have no stored return
date. Their read path derives an expected date from the current calendar.
"""
from django.db import migrations, models


DEFINITIONS = (
    (
        "staff.leave.groups", "Staff Leave Groups",
        "Named groups assigned to staff for leave allowance exceptions.",
    ),
    (
        "staff.leave.overrides", "Leave Allowance Exceptions",
        "Leave type allowances for a branch, a staff leave group, or both.",
    ),
)


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for key, label, description in DEFINITIONS:
        definition_model.objects.get_or_create(
            key=key,
            defaults={
                "label": label, "description": description,
                "value_type": "JSON", "default_value": [],
                "allowed_scopes": ["platform", "school"],
                "validation_rules": {}, "sensitivity": "INTERNAL", "is_active": True,
            },
        )


def withdraw(apps, schema_editor):
    keys = [row[0] for row in DEFINITIONS]
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=keys).delete()
    definition_model.objects.filter(key__in=keys).delete()


class Migration(migrations.Migration):
    dependencies = [("vs_staff", "0013_a_leave_request_names_who_it_is_for")]

    operations = [
        migrations.AddField(
            model_name="staffprofile", name="leave_group",
            field=models.CharField(blank=True, default="", max_length=36),
        ),
        migrations.AddField(
            model_name="leaverequest", name="resumption_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(declare, withdraw),
    ]
