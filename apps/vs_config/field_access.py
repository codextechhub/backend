"""Field Access declarations for platform settings and entitlement windows."""

from vs_rbac.field_registry import FieldSpec, register_fields


def register():
    profile_fields = (
        ("tagline", "Tagline", "tagline"),
        ("address", "Address", "address"),
        ("email", "Email", "email"),
        ("phone", "Phone", "phone"),
        ("website", "Website", "website"),
        ("logo_url", "Logo", "logo_url"),
        ("default_ownership_type", "Default ownership type", "ownership_type"),
        ("default_term_structure", "Default term structure", "term_structure"),
        ("default_currency", "Default currency", "currency"),
        ("default_branch_country", "Default branch country", "branch_country"),
    )
    register_fields(
        "platform", "profile",
        fields=tuple(
            FieldSpec(name, label, group="Issuer" if index < 6 else "Onboarding",
                      scope="PLATFORM", api_names=(name, alias) if name != alias else (alias,))
            for index, (name, label, alias) in enumerate(profile_fields)
        ),
        surfaces=("vs_config.views.PlatformSettingsView",),
    )
    register_fields(
        "platform", "entitlements",
        fields=(
            FieldSpec("window", "Subscription window", group="Grant",
                      scope="PLATFORM", api_names=("starts_at", "ends_at")),
            FieldSpec("calendar", "Subscription expiry", group="Renewal",
                      scope="PLATFORM", writable=False,
                      api_names=("entries", "summary")),
        ),
        surfaces=("vs_config.serializers.CapabilityEntitlementSerializer",),
    )
