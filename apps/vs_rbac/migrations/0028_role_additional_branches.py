from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_rbac", "0027_manage_is_not_an_action")]

    operations = [
        migrations.AddField(
            model_name="tenantroletemplate",
            name="additional_branches",
            field=models.ManyToManyField(
                blank=True,
                related_name="additional_tenant_role_templates",
                to="vs_tenants.branch",
            ),
        ),
    ]
