"""A pupil can move to another branch of the school, and each move is recorded.

``StudentBranchMove`` holds one row per move: the two branches, the day, the
reason, the class placements it closed and opened, and who made it. Its id is
the reference the FAL keys the pupil's finance move by. ``ClassEnrolment.reason``
gains ``BRANCH_MOVE``, written on the placement a move opens.

A new table and a widened choice list: no existing row changes, and reversing
drops the table.
"""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0013_suspension_return_date"),
        ("vs_tenants", "0011_platform_tenant_lagos_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="classenrolment",
            name="reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PARENT_REQUEST", "Parent request"),
                    ("STREAM_CHANGE", "Stream change"),
                    ("CLASS_BALANCING", "Class balancing"),
                    ("BEHAVIOUR", "Behaviour"),
                    ("ACADEMIC_PLACEMENT", "Academic placement"),
                    ("BRANCH_MOVE", "Moved branch"),
                    ("OTHER", "Other"),
                ],
                default="",
                max_length=24,
            ),
        ),
        migrations.CreateModel(
            name="StudentBranchMove",
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
                ("effective_date", models.DateField()),
                ("reason", models.CharField(max_length=300)),
                (
                    "from_branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "from_enrolment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="vs_students.classenrolment",
                    ),
                ),
                (
                    "moved_by",
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
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="branch_moves",
                        to="vs_students.student",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="student_branch_moves",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "to_branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "to_enrolment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="vs_students.classenrolment",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "student"],
                        name="vs_students_tenant__93f989_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("from_branch", models.F("to_branch")), _negated=True
                        ),
                        name="ck_branch_move_changes_branch",
                    )
                ],
            },
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
    ]
