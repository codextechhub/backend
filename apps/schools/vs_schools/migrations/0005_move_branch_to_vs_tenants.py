"""Hand Branch and BranchLifecycle over to vs_tenants. Model state only.

Phase D of docs/architecture/school-decoupling-scope.md, last step.

Nothing here reaches the database. The class name, the integer primary key and
the table are all unchanged, so the model that ``vs_tenants`` created and the
one this deletes are the same rows in the same table under the same name. The
wrapper is what makes that safe: unwrapped, this migration emits

    ALTER TABLE "vs_schools_branch" DROP COLUMN "tenant_id" CASCADE;
    DROP TABLE "vs_schools_branchlifecycle" CASCADE;
    DROP TABLE "vs_schools_branch" CASCADE;

which is every branch and every foreign key pointing at one.

``dependencies`` is the load-bearing part. This must run after all nine
retarget migrations, or a fresh database replays into a state where eight apps
hold foreign keys to a model that no longer exists. On an existing database the
mistake is invisible, because those apps' state was already updated by the time
anyone looks; it only bites a clean install, which is why the dependency list
is written out in full rather than left to the autodetector.

``BranchPrimaryAdmin`` stays behind: it is invite and onboarding machinery with
school defaults, and nothing outside this app refers to it. Its foreign key is
simply repointed at the model's new home.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("vs_schools", "0004_branch_drop_school"),
        ("vs_tenants", "0004_move_branch_from_vs_schools"),
        # Every app that holds a branch foreign key. The model may not leave
        # this app's state until all of them point somewhere else.
        ("vs_config", "0008_retarget_branch_to_vs_tenants"),
        ("vs_finance", "0022_retarget_branch_to_vs_tenants"),
        ("vs_import_data", "0006_retarget_branch_to_vs_tenants"),
        ("vs_procurement", "0030_retarget_branch_to_vs_tenants"),
        ("vs_rbac", "0004_retarget_branch_to_vs_tenants"),
        ("vs_tickets", "0005_retarget_branch_to_vs_tenants"),
        ("vs_user", "0005_retarget_branch_to_vs_tenants"),
        ("vs_workflow", "0008_retarget_branch_to_vs_tenants"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="branchprimaryadmin",
                    name="branch",
                    field=models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="primary_admin",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.RemoveField(
                    model_name="branch",
                    name="tenant",
                ),
                migrations.DeleteModel(
                    name="BranchLifecycle",
                ),
                migrations.DeleteModel(
                    name="Branch",
                ),
            ],
        ),
    ]
