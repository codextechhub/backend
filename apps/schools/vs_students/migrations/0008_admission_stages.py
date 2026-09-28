"""Let a school name its own admission stages, and require documents before confirming.

``AdmissionStage`` is a school's own step in its admissions, in its own order,
and ``Student`` gains three columns that say where an applicant stands:
``admission_stage``, ``stage_entered_on`` and ``offer_expires_on``. All three
are null on every existing student, which is exactly an applicant at a school
that has named no stages, so nothing reads differently until a school names
one.

One school-scoped definition is created:

* ``applicants.documents.required_to_confirm``: the documents an applicant must
  hold before being confirmed (default none, so confirming is refused for
  nobody until a school chooses).

Declared by the students module for the reason ``0006`` gives, and with the
same shape ``seed_config_catalogue`` gives it.

Reversible: the definition and every school's value of it are removed, the
three columns are dropped and the stages with them, which loses every school's
stages and where each applicant stood among them. The applicants themselves,
and their statuses, are untouched.
"""

import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models


SCHOOL = ["platform", "school"]

DEFINITION = {
    "key": "applicants.documents.required_to_confirm",
    "label": "Documents Required To Confirm An Applicant",
    "description": (
        "The documents an applicant must have on their record before they can "
        "be confirmed as enrolled, on every route that confirms one. Empty "
        "means confirming never waits for a document. Enrolling a child "
        "directly, without saving them as an applicant first, is not checked."
    ),
    "value_type": "JSON", "default_value": [],
    "validation_rules": {}, "allowed_scopes": SCHOOL,
}


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    fields = {k: v for k, v in DEFINITION.items() if k != "key"}
    definition_model.objects.get_or_create(
        key=DEFINITION["key"],
        defaults={**fields, "sensitivity": "INTERNAL", "is_active": True},
    )


def withdraw(apps, schema_editor):
    """Remove the definition and every value of it, values first.

    For the reason ``0006`` gives: an orphaned value would resurrect a school's
    choice if the key were declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key=DEFINITION["key"]).delete()
    definition_model.objects.filter(key=DEFINITION["key"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0007_guardian_settings"),
        ("vs_tenants", "0010_remove_branch__type"),
        ("vs_config", "0011_seed_workflow_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="student",
            name="offer_expires_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="student",
            name="stage_entered_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="AdmissionStage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=40)),
                ("position", models.PositiveSmallIntegerField(default=1)),
                ("is_offer", models.BooleanField(default=False)),
                (
                    "offer_valid_days",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="admission_stages",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["position", "id"],
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
            },
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddField(
            model_name="student",
            name="admission_stage",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="students",
                to="vs_students.admissionstage",
            ),
        ),
        migrations.AddIndex(
            model_name="student",
            index=models.Index(
                fields=["tenant", "status", "admission_stage"],
                name="vs_students_tenant__d8f91d_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="admissionstage",
            index=models.Index(
                fields=["tenant", "position"], name="vs_students_tenant__ee68ce_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="admissionstage",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                models.F("tenant"),
                name="uq_admission_stage_tenant_name_ci",
            ),
        ),
        migrations.RunPython(declare, withdraw),
    ]
