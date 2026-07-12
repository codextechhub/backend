import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0001_initial"),
        ("vs_user", "0002_add_unique_org_node_name_constraint"),
    ]
    operations = [
        migrations.AddField(
            model_name="user",
            name="persona",
            field=models.CharField(blank=True, choices=[("STUDENT", "Student"), ("PARENT", "Parent/Guardian"), ("STAFF", "Staff")], default="", help_text="Domain persona only; never grants authority.", max_length=16),
        ),
        migrations.AddField(
            model_name="user",
            name="tenant",
            field=models.ForeignKey(blank=True, help_text="Canonical home tenant; temporarily nullable during migration.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="users", to="vs_tenants.tenant"),
        ),
    ]
