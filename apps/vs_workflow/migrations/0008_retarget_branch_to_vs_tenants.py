"""Point this app's branch foreign keys at vs_tenants.Branch. State only.

Phase D of docs/architecture/school-decoupling-scope.md.

``Branch`` kept its class name, its integer primary key and its table
(``vs_schools_branch``), so the 3 ``branch_id`` columns here already
reference exactly the right rows. Only Django's idea of which model owns
them changes, and ``sqlmigrate`` on this migration prints nothing but
BEGIN/COMMIT.

The table name is what makes that true, not the wrapper. Django's
``BaseDatabaseSchemaEditor._field_should_be_altered`` ignores a changed
foreign key target when the old and new models share a ``db_table``, so these
would emit no SQL even unwrapped. ``SeparateDatabaseAndState`` is here to say
so out loud and to keep the whole phase one shape: it is *not* optional in
``vs_tenants.0004`` and ``vs_schools.0005``, where the same rename would
otherwise ``CREATE TABLE`` and ``DROP TABLE vs_schools_branch``.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0004_move_branch_from_vs_schools"),
        ("vs_workflow", "0007_workflowtemplate_is_active_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="workflowapprovergroup",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workflow_approver_groups",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="workflowinstance",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workflow_instances",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="workflowtemplate",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workflow_templates",
                        to="vs_tenants.branch",
                    ),
                ),
            ],
        ),
    ]
