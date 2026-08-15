"""Give Branch and BranchLifecycle their new home. Model state only.

Phase D of docs/architecture/school-decoupling-scope.md.

``Branch`` is the platform's *site* primitive: a campus, a clinic, a store.
It lived in the schools app only because that is where it was first needed,
and six engine apps had to import a product app to declare a foreign key to
it. It moves here with its own tenant, which it has carried since phase B.

**Nothing here touches the database.** The class name, the ``BigAutoField``
primary key and the table are all unchanged, so ``vs_schools_branch`` and its
41 inbound foreign key constraints stay exactly as they are and no row moves.
``SeparateDatabaseAndState`` is not decoration here: a plain ``CreateModel``
emits ``CREATE TABLE`` against a table that already exists, and the matching
``DeleteModel`` in ``vs_schools.0005`` emits ``DROP TABLE vs_schools_branch
CASCADE``, which would take every branch and every foreign key with it.
``sqlmigrate`` on this migration prints nothing but BEGIN/COMMIT.

The ``db_table`` in ``options`` is not decoration. Without it Django would
look for ``vs_tenants_branch``, and every query and every later migration
would target a table that does not exist. Renaming the table for tidiness is
deliberately not done here: it would rewrite 41 constraints for a cosmetic
gain, and it is separable.

The three composite indexes and the two constraints are declared, but were
built by ``vs_schools.0004_branch_drop_school`` under exactly these names,
which is why this migration depends on it.
"""
import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0003_tenantdocumentsequence"),
        # The indexes below already exist under these names, and the school
        # column is already gone, because that migration did the real work.
        ("vs_schools", "0004_branch_drop_school"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="Branch",
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
                            "name",
                            models.CharField(
                                help_text="Branch display name, e.g., 'Lekki Campus'",
                                max_length=255,
                            ),
                        ),
                        (
                            "code",
                            models.PositiveIntegerField(
                                db_index=True,
                                editable=False,
                                help_text="Branch code unique per tenant (1..N).",
                            ),
                        ),
                        (
                            "is_main",
                            models.BooleanField(
                                default=False,
                                help_text="Marks the primary/main branch for this tenant.",
                            ),
                        ),
                        ("_type", models.CharField(max_length=80)),
                        ("address", models.CharField(blank=True, default="", max_length=255)),
                        ("email", models.EmailField(blank=True, default="", max_length=254)),
                        ("country", models.CharField(default="Nigeria", max_length=80)),
                        ("state", models.CharField(blank=True, default="", max_length=120)),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("ACTIVE", "Active"),
                                    ("PENDING", "Pending Activation"),
                                    ("SUSPENDED", "Suspended"),
                                    ("INACTIVE", "Inactive"),
                                    ("CLOSED", "Closed"),
                                ],
                                db_index=True,
                                default="PENDING",
                                max_length=16,
                            ),
                        ),
                        ("opened_at", models.DateTimeField(blank=True, null=True)),
                        ("closed_at", models.DateTimeField(blank=True, null=True)),
                        ("activated_at", models.DateTimeField(blank=True, null=True)),
                        ("deactivated_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "created_at",
                            models.DateTimeField(
                                default=django.utils.timezone.now, editable=False
                            ),
                        ),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        (
                            "tenant",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="branches",
                                to="vs_tenants.tenant",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "vs_schools_branch",
                        "ordering": ["-created_at"],
                        "base_manager_name": "all_objects",
                        "default_manager_name": "objects",
                    },
                    managers=[
                        ("objects", django.db.models.manager.Manager()),
                        ("all_objects", django.db.models.manager.Manager()),
                    ],
                ),
                migrations.CreateModel(
                    name="BranchLifecycle",
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
                            "from_state",
                            models.CharField(
                                choices=[
                                    ("ACTIVE", "Active"),
                                    ("PENDING", "Pending Activation"),
                                    ("SUSPENDED", "Suspended"),
                                    ("INACTIVE", "Inactive"),
                                    ("CLOSED", "Closed"),
                                ],
                                max_length=32,
                            ),
                        ),
                        (
                            "to_state",
                            models.CharField(
                                choices=[
                                    ("ACTIVE", "Active"),
                                    ("PENDING", "Pending Activation"),
                                    ("SUSPENDED", "Suspended"),
                                    ("INACTIVE", "Inactive"),
                                    ("CLOSED", "Closed"),
                                ],
                                max_length=32,
                            ),
                        ),
                        ("actor_id", models.CharField(max_length=120)),
                        ("reason", models.TextField(blank=True, default="")),
                        (
                            "occurred_at",
                            models.DateTimeField(
                                db_index=True, default=django.utils.timezone.now
                            ),
                        ),
                        (
                            "branch",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="lifecycle_events",
                                to="vs_tenants.branch",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "vs_schools_branchlifecycle",
                    },
                ),
                migrations.AddIndex(
                    model_name="branch",
                    index=models.Index(
                        fields=["tenant", "is_main"], name="vs_schools__tenant__6bef02_idx"
                    ),
                ),
                migrations.AddIndex(
                    model_name="branch",
                    index=models.Index(
                        fields=["tenant", "status"], name="vs_schools__tenant__b47bb3_idx"
                    ),
                ),
                migrations.AddIndex(
                    model_name="branch",
                    index=models.Index(
                        fields=["tenant", "code"], name="vs_schools__tenant__457ea7_idx"
                    ),
                ),
                migrations.AddConstraint(
                    model_name="branch",
                    constraint=models.UniqueConstraint(
                        fields=("tenant", "code"), name="uq_branch_tenant_code"
                    ),
                ),
                migrations.AddConstraint(
                    model_name="branch",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("is_main", True)),
                        fields=("tenant",),
                        name="uq_branch_one_main_per_tenant",
                    ),
                ),
                migrations.AddIndex(
                    model_name="branchlifecycle",
                    index=models.Index(
                        fields=["branch", "occurred_at"], name="vs_schools__branch__111587_idx"
                    ),
                ),
                migrations.AddIndex(
                    model_name="branchlifecycle",
                    index=models.Index(
                        fields=["branch", "to_state"], name="vs_schools__branch__ced428_idx"
                    ),
                ),
            ],
        ),
    ]
