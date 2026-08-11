"""Curated platform settings and their runtime resolution rules.

The generic configuration catalogue remains the storage engine. This module is
the product contract for the Platform Settings screen and for code that consumes
the settings. Keeping the keys here prevents the UI and runtime services from
drifting into separate interpretations of the same values.
"""

from django.conf import settings

from .models import ConfigurationDefinition, ConfigurationValue


PROFILE_FIELDS = {
    "name": "platform.profile.name",
    "tagline": "platform.profile.tagline",
    "address": "platform.profile.address",
    "email": "platform.profile.email",
    "phone": "platform.profile.phone",
    "website": "platform.profile.website",
    "logo_url": "platform.profile.logo_url",
}

ONBOARDING_FIELDS = {
    "ownership_type": "platform.onboarding.default_ownership_type",
    "term_structure": "platform.onboarding.default_term_structure",
    "currency": "platform.onboarding.default_currency",
    "branch_country": "platform.onboarding.default_branch_country",
}

ALL_FIELDS = {**PROFILE_FIELDS, **ONBOARDING_FIELDS}

ONBOARDING_PRODUCT_DEFAULTS = {
    "ownership_type": "PUBLIC",
    "term_structure": "3_TERMS",
    "currency": "NGN",
    "branch_country": "Nigeria",
}


def _platform_rows(keys):
    definitions = {
        item.key: item
        for item in ConfigurationDefinition.objects.filter(key__in=keys, is_active=True)
    }
    values = {
        item.definition.key: item
        for item in ConfigurationValue.all_objects.select_related("definition").filter(
            definition__key__in=keys,
            definition__is_active=True,
            scope_key="platform",
        )
    }
    return definitions, values


def resolve_platform_settings():
    """Return curated values together with the source used for each field."""
    definitions, values = _platform_rows(ALL_FIELDS.values())
    issuer_fallback = getattr(settings, "PLATFORM_ISSUER", {}) or {}
    result = {"profile": {}, "onboarding": {}, "sources": {"profile": {}, "onboarding": {}}}

    for field, key in PROFILE_FIELDS.items():
        row = values.get(key)
        definition = definitions.get(key)
        if row is not None:
            value, source = row.value, "database"
        elif issuer_fallback.get(field):
            value, source = issuer_fallback[field], "environment"
        else:
            value = definition.default_value if definition is not None else ""
            source = "default"
        result["profile"][field] = value or ""
        result["sources"]["profile"][field] = source

    for field, key in ONBOARDING_FIELDS.items():
        row = values.get(key)
        definition = definitions.get(key)
        value = (
            row.value
            if row is not None
            else definition.default_value
            if definition and definition.default_value is not None
            else ONBOARDING_PRODUCT_DEFAULTS[field]
        )
        result["onboarding"][field] = value
        result["sources"]["onboarding"][field] = "database" if row is not None else "default"

    return result


def get_platform_profile():
    """Identity printed when the platform itself is the document issuer."""
    return resolve_platform_settings()["profile"]


def get_school_onboarding_defaults():
    """Defaults used only while creating new schools and branches."""
    return resolve_platform_settings()["onboarding"]
