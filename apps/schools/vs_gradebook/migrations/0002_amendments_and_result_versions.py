"""Amendments, the versions of a published result, and who amended a mark.

Creates ``Amendment`` (a correction to one subject of a published result) and
``ResultVersion`` (every snapshot ever issued for a student's result), and gives
a score the person and time of the correction that last changed it.

A result published before this migration has its one snapshot only on
``StudentTermResult``. The data step writes that snapshot as version 1 in
``ResultVersion``, so every snapshot ever issued has a version row and an
amendment has an earlier version to sit beside. It writes none for a result
that was never published, and none for a result that already has a version 1,
so running it twice adds nothing.

Reversible: the version rows are removed and the tables dropped.
"""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


BATCH = 500


def write_version_one(apps, schema_editor):
    """Give every published result its snapshot as version 1."""
    Result = apps.get_model("vs_gradebook", "StudentTermResult")
    Version = apps.get_model("vs_gradebook", "ResultVersion")
    done = set(
        Version._default_manager.filter(version=1).values_list("result_id", flat=True)
    )
    pending = []
    for result in (
        Result._default_manager.filter(snapshot__isnull=False).order_by("pk").iterator(
            chunk_size=BATCH,
        )
    ):
        if result.pk in done:
            continue
        pending.append(Version(
            tenant_id=result.tenant_id, result_id=result.pk,
            version=result.version or 1, snapshot=result.snapshot,
        ))
        if len(pending) >= BATCH:
            Version._default_manager.bulk_create(pending)
            pending = []
    if pending:
        Version._default_manager.bulk_create(pending)


def remove_versions(apps, schema_editor):
    apps.get_model("vs_gradebook", "ResultVersion")._default_manager.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_academics", "0011_electives_and_department_head"),
        ("vs_gradebook", "0001_initial"),
        ("vs_students", "0014_a_pupil_moves_branch"),
        ("vs_tenants", "0012_branch_phone"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="score",
            name="amended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="score",
            name="amended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="Amendment",
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
                ("reason", models.TextField()),
                ("before", models.JSONField(default=list)),
                ("after", models.JSONField(default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                        ],
                        default="PENDING",
                        max_length=10,
                    ),
                ),
                ("raised_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_note", models.TextField(blank=True, default="")),
                (
                    "class_term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="amendments",
                        to="vs_gradebook.classterm",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "raised_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_amendments",
                        to="vs_students.student",
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_amendments",
                        to="vs_academics.subject",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_amendments",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
            },
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="ResultVersion",
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
                ("version", models.PositiveSmallIntegerField()),
                ("snapshot", models.JSONField()),
                (
                    "amendment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="result_versions",
                        to="vs_gradebook.amendment",
                    ),
                ),
                (
                    "result",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="versions",
                        to="vs_gradebook.studenttermresult",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_result_versions",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["result_id", "version"],
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
            },
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddIndex(
            model_name="amendment",
            index=models.Index(
                fields=["tenant", "status", "raised_at"],
                name="vs_gradeboo_tenant__22d02b_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="amendment",
            index=models.Index(
                fields=["class_term", "student"], name="vs_gradeboo_class_t_6f1a6d_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="amendment",
            index=models.Index(
                fields=["tenant", "student"], name="vs_gradeboo_tenant__d9fa4e_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="amendment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "PENDING")),
                fields=("class_term", "student", "subject"),
                name="uq_gradebook_amendment_pending",
            ),
        ),
        migrations.AddIndex(
            model_name="resultversion",
            index=models.Index(
                fields=["tenant", "result"], name="vs_gradeboo_tenant__8bd41a_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="resultversion",
            constraint=models.UniqueConstraint(
                fields=("result", "version"), name="uq_gradebook_result_version"
            ),
        ),
        migrations.AddConstraint(
            model_name="resultversion",
            constraint=models.UniqueConstraint(
                fields=("amendment", "result"),
                name="uq_gradebook_result_version_amendment",
            ),
        ),
        migrations.RunPython(write_version_one, remove_versions),
    ]
