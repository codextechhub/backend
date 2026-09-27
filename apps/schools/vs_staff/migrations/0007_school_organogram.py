"""The school's own organogram: units, posts, appointments and dotted lines.

Four new tables and nothing else. No existing row is read or written, so the
reverse is simply dropping them.
"""

import django.core.validators
import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0006_staff_additional_postings"),
        ("vs_tenants", "0010_remove_branch__type"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffOrgNode",
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
                ("name", models.CharField(max_length=150)),
                ("code", models.CharField(max_length=40)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("DIVISION", "Division"),
                            ("DEPARTMENT", "Department"),
                            ("TEAM", "Team"),
                        ],
                        default="DEPARTMENT",
                        max_length=16,
                    ),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                (
                    "branch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_org_nodes",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="children",
                        to="vs_staff.stafforgnode",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_org_nodes",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["name", "id"],
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
            name="StaffPosition",
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
                ("title", models.CharField(max_length=150)),
                ("code", models.CharField(max_length=40)),
                (
                    "headcount",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "org_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="positions",
                        to="vs_staff.stafforgnode",
                    ),
                ),
                (
                    "reports_to",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="direct_reports",
                        to="vs_staff.staffposition",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_positions",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["title", "id"],
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
            model_name="stafforgnode",
            name="head_position",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="heads_units",
                to="vs_staff.staffposition",
            ),
        ),
        migrations.CreateModel(
            name="StaffMatrixReport",
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
                (
                    "relationship_label",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_matrix_reports",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "position",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dotted_lines",
                        to="vs_staff.staffposition",
                    ),
                ),
                (
                    "reports_to",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dotted_reports",
                        to="vs_staff.staffposition",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
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
            name="StaffPositionAssignment",
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
                ("is_primary", models.BooleanField(default=True)),
                ("is_acting", models.BooleanField(default=False)),
                (
                    "start_date",
                    models.DateField(default=django.utils.timezone.localdate),
                ),
                ("end_date", models.DateField(blank=True, null=True)),
                (
                    "position",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="appointments",
                        to="vs_staff.staffposition",
                    ),
                ),
                (
                    "staff",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="appointments",
                        to="vs_staff.staffprofile",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_position_assignments",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-start_date", "-id"],
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
            model_name="staffposition",
            index=models.Index(
                fields=["tenant", "is_active"], name="vs_staff_st_tenant__56d8b0_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffposition",
            index=models.Index(
                fields=["org_node", "is_active"], name="vs_staff_st_org_nod_b8a5b9_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffposition",
            index=models.Index(
                fields=["tenant", "reports_to"], name="vs_staff_st_tenant__d097f4_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffposition",
            index=models.Index(
                fields=["tenant", "title"], name="vs_staff_st_tenant__b5ad95_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="staffposition",
            constraint=models.UniqueConstraint(
                fields=("tenant", "code"), name="uq_staff_position_code"
            ),
        ),
        migrations.AddIndex(
            model_name="stafforgnode",
            index=models.Index(
                fields=["tenant", "is_active"], name="vs_staff_st_tenant__4b70ad_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="stafforgnode",
            index=models.Index(
                fields=["tenant", "parent"], name="vs_staff_st_tenant__180029_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="stafforgnode",
            index=models.Index(
                fields=["tenant", "kind"], name="vs_staff_st_tenant__104abd_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="stafforgnode",
            index=models.Index(
                fields=["tenant", "branch"], name="vs_staff_st_tenant__6427f5_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="stafforgnode",
            constraint=models.UniqueConstraint(
                fields=("tenant", "code"), name="uq_staff_org_node_code"
            ),
        ),
        migrations.AddIndex(
            model_name="staffmatrixreport",
            index=models.Index(
                fields=["tenant", "position"], name="vs_staff_st_tenant__b04a27_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffmatrixreport",
            index=models.Index(
                fields=["tenant", "reports_to"], name="vs_staff_st_tenant__8bcdc0_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="staffmatrixreport",
            constraint=models.UniqueConstraint(
                fields=("position", "reports_to"), name="uq_staff_matrix_report"
            ),
        ),
        migrations.AddIndex(
            model_name="staffpositionassignment",
            index=models.Index(
                fields=["tenant", "end_date"], name="vs_staff_st_tenant__b42909_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffpositionassignment",
            index=models.Index(
                fields=["position", "end_date"], name="vs_staff_st_positio_4b1817_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffpositionassignment",
            index=models.Index(
                fields=["staff", "end_date"], name="vs_staff_st_staff_i_e26efb_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="staffpositionassignment",
            index=models.Index(
                fields=["tenant", "-start_date"], name="vs_staff_st_tenant__1871ff_idx"
            ),
        ),
    ]
