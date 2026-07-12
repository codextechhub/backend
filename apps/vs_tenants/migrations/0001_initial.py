import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Tenant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=80, unique=True, validators=[django.core.validators.RegexValidator(message="Slug must be lowercase letters/numbers separated by single hyphens.", regex="^[a-z0-9]+(?:-[a-z0-9]+)*$")])),
                ("kind", models.CharField(choices=[("PLATFORM", "Platform"), ("SCHOOL", "School"), ("ORGANIZATION", "Organization")], max_length=16)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("INACTIVE", "Inactive")], db_index=True, default="PENDING", max_length=16)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("deactivated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddIndex(
            model_name="tenant",
            index=models.Index(fields=["kind", "status"], name="vs_tenants_kind_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="tenant",
            constraint=models.CheckConstraint(condition=~models.Q(slug=""), name="tenant_slug_not_empty"),
        ),
    ]
