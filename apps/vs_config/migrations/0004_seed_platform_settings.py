from django.db import migrations


DEFINITIONS = [
    (
        "platform.profile.name",
        "Platform name",
        "Name printed when the platform is the issuer of a Finance document.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.profile.tagline",
        "Platform tagline",
        "Short line printed below the platform name on issued documents.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.profile.address",
        "Platform address",
        "Contact address printed on platform-issued Finance documents.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.profile.email",
        "Platform email",
        "Contact email printed on platform-issued Finance documents.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.profile.phone",
        "Platform phone",
        "Contact phone printed on platform-issued Finance documents.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.profile.website",
        "Platform website",
        "Website printed on platform-issued Finance documents.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.profile.logo_url",
        "Platform logo URL",
        "Public logo URL used on platform-issued Finance documents.",
        "STRING",
        None,
        {},
    ),
    (
        "platform.onboarding.default_ownership_type",
        "Default school ownership",
        "Ownership type preselected when a new school omits the field.",
        "CHOICE",
        "PUBLIC",
        {"choices": ["PUBLIC", "PRIVATE", "FAITH_BASED", "NGO"]},
    ),
    (
        "platform.onboarding.default_term_structure",
        "Default academic structure",
        "Academic calendar structure used when a new school omits the field.",
        "CHOICE",
        "3_TERMS",
        {"choices": ["2_SEMESTERS", "3_TERMS"]},
    ),
    (
        "platform.onboarding.default_currency",
        "Default school currency",
        "Billing currency used when a new school omits the field.",
        "CHOICE",
        "NGN",
        {"choices": ["NGN", "USD"]},
    ),
    (
        "platform.onboarding.default_branch_country",
        "Default branch country",
        "Country used when a new branch omits the field.",
        "STRING",
        "Nigeria",
        {},
    ),
]


def seed_platform_settings(apps, schema_editor):
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
    dependencies = [("vs_config", "0003_configuration_audit_immutability")]

    operations = [migrations.RunPython(seed_platform_settings, migrations.RunPython.noop)]
