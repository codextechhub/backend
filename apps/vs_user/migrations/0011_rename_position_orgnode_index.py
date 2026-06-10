from django.db import migrations


class Migration(migrations.Migration):
    """
    Give the Position(org_node, is_active) index an explicit, stable name.
    A RENAME (not drop+create) so MariaDB's FK auto-index management is never
    triggered on the org_node FK column.
    """

    dependencies = [
        ('vs_user', '0010_alter_orgnode_options_and_more'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='position',
            new_name='pos_orgnode_active_idx',
            old_name='vs_users_po_org_nod_43bb45_idx',
        ),
    ]
