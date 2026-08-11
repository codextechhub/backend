from django.db import migrations


DEFINITIONS = [
    ("security.failed_login_threshold", "Failed login threshold", "Failed password attempts allowed before the account is locked.", "INTEGER", 5, {"min": 3, "max": 20}),
    ("security.account_lock_minutes", "Account lock duration", "Minutes an account remains locked after reaching the failed-login threshold.", "INTEGER", 15, {"min": 5, "max": 1440}),
    ("security.self_reset_expiry_hours", "Self-service reset lifetime", "Hours a self-service password reset link remains valid.", "INTEGER", 1, {"min": 1, "max": 24}),
    ("security.admin_reset_expiry_hours", "Admin reset lifetime", "Hours an administrator-triggered password reset link remains valid.", "INTEGER", 24, {"min": 1, "max": 168}),
    ("security.invitation_expiry_days", "Invitation lifetime", "Days a new-user invitation remains valid, including after resend.", "INTEGER", 7, {"min": 1, "max": 30}),
    ("security.proxy_idle_timeout_minutes", "Proxy session idle timeout", "Minutes an open-ended proxy session may remain idle before it expires.", "INTEGER", 30, {"min": 5, "max": 120}),
    ("integrations.email.sender_name", "Default email sender name", "Display name used for platform email when a message does not provide its own sender name.", "STRING", None, {}),
    ("integrations.email.sender_address", "Default email sender address", "From address used for platform email. SMTP credentials remain deployment-owned.", "STRING", None, {}),
    ("notifications.email_max_retries", "Email delivery max retries", "Maximum delivery retries for a queued email notification.", "INTEGER", 3, {"min": 0, "max": 10}),
    ("notifications.email_retry_backoff_seconds", "Email retry backoff seconds", "Seconds between queued email delivery retries.", "INTEGER", 60, {"min": 1, "max": 3600}),
]


def seed_runtime_settings(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for key, label, description, value_type, default_value, validation_rules in DEFINITIONS:
        definition_model.objects.get_or_create(
            key=key,
            defaults={
                "label": label,
                "description": description,
                "value_type": value_type,
                "default_value": default_value,
                "validation_rules": validation_rules,
                "allowed_scopes": ["platform"],
                "sensitivity": "INTERNAL",
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("vs_config", "0004_seed_platform_settings")]

    operations = [migrations.RunPython(seed_runtime_settings, migrations.RunPython.noop)]
