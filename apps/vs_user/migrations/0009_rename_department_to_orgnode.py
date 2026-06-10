from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Rename Department -> OrgNode (a generic, tiered org node) and add the
    DIVISION / DEPARTMENT / TEAM `kind`. Data-preserving: the table is renamed
    in place and Position.department is renamed to Position.org_node rather than
    dropped/recreated.
    """

    dependencies = [
        ('vs_user', '0008_remove_platformstaffprofile_vs_users_pl_departm_c3b068_idx_and_more'),
    ]

    operations = [
        migrations.RenameModel(old_name='Department', new_name='OrgNode'),
        migrations.AlterModelTable(name='orgnode', table='vs_users_org_node'),
        migrations.RenameField(model_name='position', old_name='department', new_name='org_node'),
        migrations.AddField(
            model_name='orgnode',
            name='kind',
            field=models.CharField(
                choices=[('DIVISION', 'Division'), ('DEPARTMENT', 'Department'), ('TEAM', 'Team')],
                default='DEPARTMENT',
                max_length=16,
            ),
        ),
    ]
