from django.db import migrations, models


class Migration(migrations.Migration):
    """
    State-only reconciliation. After the 0009 column rename (department_id ->
    org_node_id) and the 0010/0011 index renames, the PHYSICAL index
    `pos_orgnode_active_idx` already covers (org_node_id, is_active) correctly.

    Migration STATE, however, still listed the index fields as ('department',
    'is_active') — RenameIndex changes the name, not the field list — which made
    the autodetector keep emitting a drop+create. That drop fails on MariaDB
    because its FK auto-index management looks up the now-nonexistent
    `department` field.

    So we fix STATE only (no DDL): the database already matches the model.
    """

    dependencies = [
        ('vs_user', '0011_rename_position_orgnode_index'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveIndex(
                    model_name='position',
                    name='pos_orgnode_active_idx',
                ),
                migrations.AddIndex(
                    model_name='position',
                    index=models.Index(
                        fields=['org_node', 'is_active'],
                        name='pos_orgnode_active_idx',
                    ),
                ),
            ],
        ),
    ]
