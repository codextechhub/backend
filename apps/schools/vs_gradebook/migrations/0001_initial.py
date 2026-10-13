"""The gradebook: assessment setup, score sheets, results and holds.

Creates every table of the gradebook and declares its six school-scoped
settings, so a database that has only migrated (no seed run) already has them:

* ``gradebook.show_class_stats``: whether a report card prints the class
  average, highest and lowest beside each subject (default on);
* ``gradebook.rank_in_class``: whether the school ranks its students and shows
  a position anywhere (default on);
* ``gradebook.conduct_ratings``: whether the class teacher rates conduct and
  skills (default on);
* ``gradebook.rating_key``: whether a report card prints the 1 to 5 key
  (default on);
* ``gradebook.rating_traits``: the traits a class teacher rates, ten by default;
* ``gradebook.moderated_programs``: the codes of the programmes whose sheets go
  to a department head first (default none).

Every default is what a school gets before it chooses. Declared here for the
reason ``vs_attendance`` ``0002`` gives, with the shape ``seed_config_catalogue``
gives them.

Reversible: the tables are dropped, and the definitions and every school's
values of them are removed.
"""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


SCHOOL = ["platform", "school"]

DEFINITIONS = [
    {
        "key": "gradebook.show_class_stats",
        "label": "Class Statistics On Report Cards",
        "description": (
            "Whether a report card prints the class average, highest and lowest "
            "beside each subject."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "gradebook.rank_in_class",
        "label": "Position In Class",
        "description": (
            "Whether the school ranks its students. When off, no position is "
            "shown on a broadsheet, a report card, a student's results or in "
            "analytics."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "gradebook.conduct_ratings",
        "label": "Conduct And Skills Ratings",
        "description": (
            "Whether the class teacher rates each student's conduct and skills "
            "when compiling the class, and the report card prints the ratings."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "gradebook.rating_key",
        "label": "Rating Key On Report Cards",
        "description": (
            "Whether a report card prints the line explaining what each rating "
            "from 1 to 5 means."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "gradebook.rating_traits",
        "label": "Traits The Class Teacher Rates",
        "description": (
            "The conduct and skills traits rated from 1 to 5, in the order the "
            "report card prints them. One to fifteen different names."
        ),
        "value_type": "JSON",
        "default_value": [
            "Punctuality", "Neatness", "Politeness", "Honesty",
            "Relationship with others", "Attentiveness", "Handwriting",
            "Sports", "Drawing and painting", "Handling of tools",
        ],
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "gradebook.moderated_programs",
        "label": "Programmes Whose Sheets Are Moderated",
        "description": (
            "The codes of the programmes whose score sheets go to the head of "
            "the subject's department before they count. Empty means no sheet "
            "is moderated."
        ),
        "value_type": "JSON", "default_value": [],
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
]

KEYS = [row["key"] for row in DEFINITIONS]


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for row in DEFINITIONS:
        fields = {k: v for k, v in row.items() if k != "key"}
        definition_model.objects.get_or_create(
            key=row["key"],
            defaults={**fields, "sensitivity": "INTERNAL", "is_active": True},
        )


def withdraw(apps, schema_editor):
    """Remove the definitions and every value of them.

    The values go first, so an orphan cannot resurrect a school's choice if the
    key is declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("vs_academics", "0011_electives_and_department_head"),
        ("vs_config", "0016_held_tolerance_in_naira"),
        ("vs_students", "0014_a_pupil_moves_branch"),
        ("vs_tenants", "0011_platform_tenant_lagos_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AssessmentStructure",
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
                ("name", models.CharField(max_length=80)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("IN_USE", "In use"),
                            ("RETIRED", "Retired"),
                        ],
                        default="DRAFT",
                        max_length=10,
                    ),
                ),
                (
                    "branch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assessment_structures",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "copied_from",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copies",
                        to="vs_gradebook.assessmentstructure",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "program",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assessment_structures",
                        to="vs_academics.program",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assessment_structures",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["program_id", "id"],
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
            name="AssessmentComponent",
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
                ("name", models.CharField(max_length=60)),
                ("short_name", models.CharField(max_length=12)),
                ("max_score", models.PositiveSmallIntegerField()),
                ("is_exam", models.BooleanField(default=False)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assessment_components",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "structure",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="vs_gradebook.assessmentstructure",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "id"],
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
            name="ClassTerm",
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
                    "stage",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("AWAITING", "Awaiting approval"),
                            ("PUBLISHED", "Published"),
                        ],
                        max_length=10,
                        null=True,
                    ),
                ),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("returned_at", models.DateTimeField(blank=True, null=True)),
                ("returned_note", models.TextField(blank=True, default="")),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_class_terms",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "published_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "returned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "school_class",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_terms",
                        to="vs_academics.schoolclass",
                    ),
                ),
                (
                    "structure",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="class_terms",
                        to="vs_gradebook.assessmentstructure",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_class_terms",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_class_terms",
                        to="vs_academics.academicterm",
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
            name="GradingScale",
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
                ("name", models.CharField(max_length=80)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("IN_USE", "In use"),
                            ("RETIRED", "Retired"),
                        ],
                        default="DRAFT",
                        max_length=10,
                    ),
                ),
                (
                    "branch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grading_scales",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "copied_from",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copies",
                        to="vs_gradebook.gradingscale",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "program",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grading_scales",
                        to="vs_academics.program",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grading_scales",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["program_id", "id"],
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
            name="GradeBand",
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
                ("min_score", models.PositiveSmallIntegerField()),
                ("grade", models.CharField(max_length=10)),
                ("remark", models.CharField(blank=True, default="", max_length=120)),
                ("is_fail", models.BooleanField(default=False)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grade_bands",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "scale",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bands",
                        to="vs_gradebook.gradingscale",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "id"],
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
            name="PrincipalComment",
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
                ("band_min", models.PositiveSmallIntegerField()),
                ("text", models.TextField()),
                ("written_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "class_term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="principal_comments",
                        to="vs_gradebook.classterm",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_principal_comments",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "written_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
            name="RemarkTemplate",
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
                ("band_min", models.PositiveSmallIntegerField()),
                ("text", models.CharField(max_length=300)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="remark_templates",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-band_min"],
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
            name="ResultHold",
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
                ("placed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("lifted_at", models.DateTimeField(blank=True, null=True)),
                ("lift_note", models.TextField(blank=True, default="")),
                (
                    "lifted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "placed_by",
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
                        related_name="gradebook_holds",
                        to="vs_students.student",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_holds",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_holds",
                        to="vs_academics.academicterm",
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
            name="ScoreSheet",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("NOT_STARTED", "Not started"),
                            ("IN_PROGRESS", "In progress"),
                            ("SUBMITTED", "Submitted"),
                            ("APPROVED", "Approved"),
                            ("RETURNED", "Returned"),
                        ],
                        default="NOT_STARTED",
                        max_length=12,
                    ),
                ),
                ("needs_moderation", models.BooleanField(default=False)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("moderated_at", models.DateTimeField(blank=True, null=True)),
                ("returned_at", models.DateTimeField(blank=True, null=True)),
                ("returned_note", models.TextField(blank=True, default="")),
                ("saved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "class_term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sheets",
                        to="vs_gradebook.classterm",
                    ),
                ),
                (
                    "moderated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "returned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_sheets",
                        to="vs_academics.subject",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_sheets",
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
            name="Score",
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
                ("mark", models.PositiveSmallIntegerField(blank=True, null=True)),
                (
                    "marker",
                    models.CharField(
                        blank=True,
                        choices=[("ABS", "Absent"), ("EXC", "Excused")],
                        max_length=3,
                        null=True,
                    ),
                ),
                ("entered_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scores",
                        to="vs_gradebook.assessmentcomponent",
                    ),
                ),
                (
                    "entered_by",
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
                        related_name="gradebook_scores",
                        to="vs_students.student",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_scores",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "sheet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scores",
                        to="vs_gradebook.scoresheet",
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
            name="SheetReminder",
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
                ("on_date", models.DateField()),
                ("sent_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "sent_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "sheet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reminders",
                        to="vs_gradebook.scoresheet",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_reminders",
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
            name="StudentTermResult",
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
                ("teacher_comment", models.TextField(blank=True, default="")),
                ("comment_at", models.DateTimeField(blank=True, null=True)),
                ("ratings", models.JSONField(blank=True, default=dict)),
                ("snapshot", models.JSONField(blank=True, null=True)),
                ("version", models.PositiveSmallIntegerField(default=0)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                (
                    "email_state",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("SENT", "Emailed"),
                            ("NO_EMAIL", "No email on file"),
                            ("HELD", "Held, nothing sent"),
                        ],
                        max_length=10,
                        null=True,
                    ),
                ),
                ("emailed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "class_term",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="results",
                        to="vs_gradebook.classterm",
                    ),
                ),
                (
                    "comment_by",
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
                        related_name="gradebook_results",
                        to="vs_students.student",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gradebook_results",
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
        migrations.AddIndex(
            model_name="assessmentstructure",
            index=models.Index(
                fields=["tenant", "program", "status"],
                name="vs_gradeboo_tenant__7167da_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="assessmentstructure",
            constraint=models.UniqueConstraint(
                condition=models.Q(("branch__isnull", False), ("status", "IN_USE")),
                fields=("tenant", "program", "branch"),
                name="uq_gradebook_structure_in_use_branch",
            ),
        ),
        migrations.AddConstraint(
            model_name="assessmentstructure",
            constraint=models.UniqueConstraint(
                condition=models.Q(("branch__isnull", True), ("status", "IN_USE")),
                fields=("tenant", "program"),
                name="uq_gradebook_structure_in_use_school",
            ),
        ),
        migrations.AddIndex(
            model_name="assessmentcomponent",
            index=models.Index(
                fields=["structure", "order"], name="vs_gradeboo_structu_5a1da4_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="assessmentcomponent",
            constraint=models.CheckConstraint(
                condition=models.Q(("max_score__gte", 1), ("max_score__lte", 100)),
                name="ck_gradebook_component_max_score_range",
            ),
        ),
        migrations.AddIndex(
            model_name="classterm",
            index=models.Index(
                fields=["tenant", "term"], name="vs_gradeboo_tenant__183de5_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="classterm",
            index=models.Index(
                fields=["tenant", "term", "stage"],
                name="vs_gradeboo_tenant__bf18a9_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="classterm",
            index=models.Index(
                fields=["structure", "stage"], name="vs_gradeboo_structu_3fdf8b_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="classterm",
            constraint=models.UniqueConstraint(
                fields=("school_class", "term"), name="uq_gradebook_class_term"
            ),
        ),
        migrations.AddIndex(
            model_name="gradingscale",
            index=models.Index(
                fields=["tenant", "program", "status"],
                name="vs_gradeboo_tenant__239c77_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="gradingscale",
            constraint=models.UniqueConstraint(
                condition=models.Q(("branch__isnull", False), ("status", "IN_USE")),
                fields=("tenant", "program", "branch"),
                name="uq_gradebook_scale_in_use_branch",
            ),
        ),
        migrations.AddConstraint(
            model_name="gradingscale",
            constraint=models.UniqueConstraint(
                condition=models.Q(("branch__isnull", True), ("status", "IN_USE")),
                fields=("tenant", "program"),
                name="uq_gradebook_scale_in_use_school",
            ),
        ),
        migrations.AddIndex(
            model_name="gradeband",
            index=models.Index(
                fields=["scale", "order"], name="vs_gradeboo_scale_i_d6f75c_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="gradeband",
            constraint=models.CheckConstraint(
                condition=models.Q(("min_score__gte", 0), ("min_score__lte", 100)),
                name="ck_gradebook_band_min_score_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="principalcomment",
            constraint=models.UniqueConstraint(
                fields=("class_term", "band_min"),
                name="uq_gradebook_principal_comment_band",
            ),
        ),
        migrations.AddConstraint(
            model_name="remarktemplate",
            constraint=models.UniqueConstraint(
                fields=("tenant", "band_min"), name="uq_gradebook_remark_tenant_band"
            ),
        ),
        migrations.AddConstraint(
            model_name="remarktemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("band_min__gte", 0), ("band_min__lte", 100)),
                name="ck_gradebook_remark_band_range",
            ),
        ),
        migrations.AddIndex(
            model_name="resulthold",
            index=models.Index(
                fields=["tenant", "term"], name="vs_gradeboo_tenant__27e61e_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="resulthold",
            constraint=models.UniqueConstraint(
                condition=models.Q(("lifted_at__isnull", True)),
                fields=("term", "student"),
                name="uq_gradebook_hold_open_term_student",
            ),
        ),
        migrations.AddIndex(
            model_name="scoresheet",
            index=models.Index(
                fields=["tenant", "status"], name="vs_gradeboo_tenant__829ccf_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="scoresheet",
            constraint=models.UniqueConstraint(
                fields=("class_term", "subject"),
                name="uq_gradebook_sheet_class_term_subject",
            ),
        ),
        migrations.AddIndex(
            model_name="score",
            index=models.Index(
                fields=["tenant", "student"], name="vs_gradeboo_tenant__20733a_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="score",
            index=models.Index(
                fields=["component"], name="vs_gradeboo_compone_92dd46_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="score",
            constraint=models.UniqueConstraint(
                fields=("sheet", "student", "component"), name="uq_gradebook_score_cell"
            ),
        ),
        migrations.AddConstraint(
            model_name="score",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("mark__isnull", True), ("marker__isnull", True), _connector="OR"
                ),
                name="ck_gradebook_score_mark_or_marker",
            ),
        ),
        migrations.AddIndex(
            model_name="sheetreminder",
            index=models.Index(
                fields=["tenant", "on_date"], name="vs_gradeboo_tenant__664ea7_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="sheetreminder",
            constraint=models.UniqueConstraint(
                fields=("sheet", "on_date"), name="uq_gradebook_reminder_sheet_date"
            ),
        ),
        migrations.AddIndex(
            model_name="studenttermresult",
            index=models.Index(
                fields=["tenant", "student"], name="vs_gradeboo_tenant__286ebe_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="studenttermresult",
            constraint=models.UniqueConstraint(
                fields=("class_term", "student"),
                name="uq_gradebook_result_class_term_student",
            ),
        ),
        migrations.RunPython(declare, withdraw),
    ]
