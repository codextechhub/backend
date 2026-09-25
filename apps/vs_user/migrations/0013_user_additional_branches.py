from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_user", "0012_a_lockout_is_not_a_status")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The compatibility anchor of this person’s posting set. NULL means "
                    '"across the whole tenant" for a tenant user, and is the only legal value for '
                    "Vision Staff, who belong to no tenant branch at all."
                ),
                null=True,
                on_delete=models.PROTECT,
                related_name="users",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="additional_branches",
            field=models.ManyToManyField(
                blank=True, related_name="additional_users", to="vs_tenants.branch",
            ),
        ),
    ]
