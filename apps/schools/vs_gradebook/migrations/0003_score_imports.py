"""``ScoreImport``: a spreadsheet of scores that has been checked and awaits confirmation.

The record lives for a day and is used once. Reversible: the table is dropped,
which loses only uploads nobody confirmed.
"""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_academics", "0011_electives_and_department_head"),
        ("vs_gradebook", "0002_amendments_and_result_versions"),
        ("vs_tenants", "0012_branch_phone"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ScoreImport",
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
                ("token", models.CharField(max_length=64, unique=True)),
                ("fingerprint", models.CharField(max_length=64)),
                ("file_name", models.CharField(blank=True, default="", max_length=255)),
                ("rows", models.JSONField(default=list)),
                ("expires_at", models.DateTimeField()),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "school_class",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="vs_academics.schoolclass",
                    ),
                ),
                (
                    "structure",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="vs_gradebook.assessmentstructure",
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="vs_academics.subject",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gradebook_score_imports",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="vs_academics.academicterm",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["expires_at"], name="vs_gradeboo_expires_388c0e_idx"
                    ),
                    models.Index(
                        fields=["tenant", "user"], name="vs_gradeboo_tenant__cfb93c_idx"
                    ),
                ],
            },
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
    ]
