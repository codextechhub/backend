from django.db import migrations


SECURITY_KEYS = [
    "security.failed_login_threshold",
    "security.account_lock_minutes",
    "security.self_reset_expiry_hours",
    "security.admin_reset_expiry_hours",
    "security.invitation_expiry_days",
    "security.proxy_idle_timeout_minutes",
]


def enable_scoped_security(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.filter(key__in=SECURITY_KEYS).update(
        allowed_scopes=["platform", "school", "branch"],
    )


def restore_platform_only(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.filter(key__in=SECURITY_KEYS).update(
        allowed_scopes=["platform"],
    )


class Migration(migrations.Migration):
    dependencies = [("vs_config", "0005_seed_runtime_settings")]

    operations = [
        migrations.RunPython(enable_scoped_security, restore_platform_only),
    ]
