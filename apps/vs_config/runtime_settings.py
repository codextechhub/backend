"""Runtime security and integration settings owned by Platform Settings.

Only values with a real backend consumer belong here. Credentials, SMTP hosts,
callback URLs and provider endpoints remain deployment-owned. The API exposes
only safe readiness information for those boundaries and never returns secrets.
"""

from email.utils import parseaddr

from django.conf import settings

from .models import ConfigurationDefinition, ConfigurationValue
from .services.scopes import normalize_scope


SECURITY_FIELDS = {
    "failed_login_threshold": "security.failed_login_threshold",
    "account_lock_minutes": "security.account_lock_minutes",
    "self_reset_expiry_hours": "security.self_reset_expiry_hours",
    "admin_reset_expiry_hours": "security.admin_reset_expiry_hours",
    "invitation_expiry_days": "security.invitation_expiry_days",
    "proxy_idle_timeout_minutes": "security.proxy_idle_timeout_minutes",
}

SECURITY_DEFAULTS = {
    "failed_login_threshold": 5,
    "account_lock_minutes": 15,
    "self_reset_expiry_hours": 1,
    "admin_reset_expiry_hours": 24,
    "invitation_expiry_days": 7,
    "proxy_idle_timeout_minutes": 30,
}

INTEGRATION_FIELDS = {
    "email_sender_name": "integrations.email.sender_name",
    "email_sender_address": "integrations.email.sender_address",
    "email_max_retries": "notifications.email_max_retries",
    "email_retry_backoff_seconds": "notifications.email_retry_backoff_seconds",
}

INTEGRATION_DEFAULTS = {
    "email_max_retries": 3,
    "email_retry_backoff_seconds": 60,
}

SPECIAL_MANAGED_KEYS = frozenset((*SECURITY_FIELDS.values(), *INTEGRATION_FIELDS.values()))
PRODUCT_OWNED_KEYS = SPECIAL_MANAGED_KEYS

# A scoped security value may only be as strict as, or stricter than, its
# parent. This prevents a school or branch from weakening the platform's
# compliance baseline while still allowing local risk controls.
SECURITY_COMPLIANCE = {
    "failed_login_threshold": {"direction": "maximum", "min": 3, "max": 20},
    "account_lock_minutes": {"direction": "minimum", "min": 5, "max": 1440},
    "self_reset_expiry_hours": {"direction": "maximum", "min": 1, "max": 24},
    "admin_reset_expiry_hours": {"direction": "maximum", "min": 1, "max": 168},
    "invitation_expiry_days": {"direction": "maximum", "min": 1, "max": 30},
    "proxy_idle_timeout_minutes": {"direction": "maximum", "min": 5, "max": 120},
}

# Code-owned ownership data. Administrators can see who consumes a key, but
# cannot author claims that are not backed by an application integration.
SETTING_CONSUMERS = {
    "platform.profile.name": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the issuer identity on platform invoices and receipts.",
    },
    "platform.profile.tagline": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the issuer tagline on platform invoices and receipts.",
    },
    "platform.profile.address": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the issuer address on platform invoices and receipts.",
    },
    "platform.profile.email": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the public issuer email on platform invoices and receipts.",
    },
    "platform.profile.phone": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the public issuer phone on platform invoices and receipts.",
    },
    "platform.profile.website": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the issuer website on platform invoices and receipts.",
    },
    "platform.profile.logo_url": {
        "service": "Finance documents",
        "consumer": "vs_finance.documents._issuer_block",
        "impact": "Supplies the public logo URL on platform invoices and receipts.",
    },
    "platform.onboarding.default_ownership_type": {
        "service": "School onboarding",
        "consumer": "vs_schools.serializers.SchoolCreateSerializer",
        "impact": "Fills ownership type when a new school omits it.",
    },
    "platform.onboarding.default_term_structure": {
        "service": "School onboarding",
        "consumer": "vs_schools.serializers.SchoolCreateSerializer",
        "impact": "Fills academic structure when a new school omits it.",
    },
    "platform.onboarding.default_currency": {
        "service": "School onboarding",
        "consumer": "vs_schools.serializers.SchoolCreateSerializer",
        "impact": "Fills billing currency when a new school omits it.",
    },
    "platform.onboarding.default_branch_country": {
        "service": "Branch onboarding",
        "consumer": "vs_schools.serializers.BranchCreateSerializer",
        "impact": "Fills country when a new branch omits it.",
    },
    "security.failed_login_threshold": {
        "service": "User authentication",
        "consumer": "vs_user.services.auth.LoginService",
        "impact": "Controls when failed sign-ins lock an account.",
    },
    "security.account_lock_minutes": {
        "service": "User authentication",
        "consumer": "vs_user.services.auth.LoginService",
        "impact": "Controls the duration of automatic account lockouts.",
    },
    "security.self_reset_expiry_hours": {
        "service": "Password recovery",
        "consumer": "vs_user.services.password.PasswordService",
        "impact": "Controls self-service password reset link expiry.",
    },
    "security.admin_reset_expiry_hours": {
        "service": "Password recovery",
        "consumer": "vs_user.services.password.PasswordService",
        "impact": "Controls administrator-issued password reset link expiry.",
    },
    "security.invitation_expiry_days": {
        "service": "User invitations",
        "consumer": "vs_user.services.invitation.InvitationService",
        "impact": "Controls new-user invitation expiry.",
    },
    "security.proxy_idle_timeout_minutes": {
        "service": "Proxy sessions",
        "consumer": "vs_rbac.authentication.TenantJWTAuthentication",
        "impact": "Expires idle impersonation sessions during authentication.",
    },
    "integrations.email.sender_name": {
        "service": "Application mail",
        "consumer": "core.mail.build_from_email",
        "impact": "Supplies the default display name for outbound email.",
    },
    "integrations.email.sender_address": {
        "service": "Application mail",
        "consumer": "core.mail.build_from_email",
        "impact": "Supplies the default sender address for outbound email.",
    },
    "notifications.email_max_retries": {
        "service": "Notification worker",
        "consumer": "vs_notifications.tasks",
        "impact": "Limits queued email delivery retries.",
    },
    "notifications.email_retry_backoff_seconds": {
        "service": "Notification worker",
        "consumer": "vs_notifications.tasks",
        "impact": "Controls the delay between email delivery retries.",
    },
}


def _scoped_values(keys, *, tenant=None, branch=None):
    tenant, branch = normalize_scope(tenant=tenant, branch=branch)
    definitions = {
        row.key: row
        for row in ConfigurationDefinition.objects.filter(key__in=keys, is_active=True)
    }
    scope_keys = []
    if branch is not None:
        scope_keys.append(f"branch:{branch.pk}")
    if tenant is not None:
        scope_keys.append(f"tenant:{tenant.pk}")
    scope_keys.append("platform")
    rows = ConfigurationValue.all_objects.select_related("definition").filter(
        definition__key__in=keys,
        definition__is_active=True,
        scope_key__in=scope_keys,
    )
    by_key_and_scope = {(row.definition.key, row.scope_key): row for row in rows}
    values = {}
    for key in keys:
        values[key] = next(
            (by_key_and_scope[(key, scope)] for scope in scope_keys if (key, scope) in by_key_and_scope),
            None,
        )
    return definitions, values


def _deployment_sender():
    name, address = parseaddr(getattr(settings, "DEFAULT_FROM_EMAIL", ""))
    return name or "CodeX System", address


def _scope_label(scope_key):
    if not scope_key:
        return "default"
    if scope_key == "platform":
        return "platform"
    if scope_key.startswith("tenant:"):
        return "school"
    return "branch"


def _current_scope_key(*, tenant=None, branch=None):
    if branch is not None:
        return f"branch:{branch.pk}"
    if tenant is not None:
        return f"tenant:{tenant.pk}"
    return "platform"


def resolve_security_settings(*, tenant=None, branch=None):
    """Return effective security values plus the source of every value.

    ``settings`` is always the **effective** value: a scoped override that is
    weaker than its parent baseline is clamped back to that baseline here, at
    read time. Write-time validation alone cannot hold the baseline, because a
    tenant override saved while the platform was lax stays in the database when
    the platform later tightens - and every consumer reads through this
    function, so clamping here closes that gap for all of them at once. The
    value as stored is preserved under ``configured`` so administration screens
    can still show what was chosen and why it is not in force.
    """
    tenant, branch = normalize_scope(tenant=tenant, branch=branch)
    definitions, values = _scoped_values(SECURITY_FIELDS.values(), tenant=tenant, branch=branch)
    current_scope = _current_scope_key(tenant=tenant, branch=branch)
    result = {
        "settings": {}, "configured": {}, "sources": {}, "source_scopes": {},
        "overrides": {}, "compliance": {},
        "scope": {
            "type": _scope_label(current_scope),
            "tenant": str(tenant.pk) if tenant is not None else None,
            "branch": str(branch.pk) if branch is not None else None,
        },
    }
    for field, key in SECURITY_FIELDS.items():
        row = values.get(key)
        definition = definitions.get(key)
        if row is not None:
            value, source = row.value, "database"
        elif definition is not None and definition.default_value is not None:
            value, source = definition.default_value, "default"
        else:
            value, source = SECURITY_DEFAULTS[field], "default"
        result["settings"][field] = int(value)
        result["configured"][field] = int(value)
        result["sources"][field] = source
        result["source_scopes"][field] = _scope_label(row.scope_key if row else None)
        result["overrides"][field] = bool(row and row.scope_key == current_scope)

    # A non-platform override is bounded by the effective parent value. Branch
    # values compare with the school layer; school values compare with platform.
    # The parent is itself resolved through this function, so the clamp is
    # transitive: a branch can never be weaker than platform via a lax school.
    if tenant is not None:
        parent = resolve_security_settings(tenant=tenant) if branch is not None else resolve_security_settings()
        for field, policy in SECURITY_COMPLIANCE.items():
            boundary = parent["settings"][field]
            configured = result["configured"][field]
            if policy["direction"] == "maximum":
                effective = min(configured, boundary)
            else:
                effective = max(configured, boundary)
            result["settings"][field] = effective
            result["compliance"][field] = {
                **policy,
                "boundary": boundary,
                "parent_scope": "school" if branch is not None else "platform",
                # True when the stored value is weaker than the baseline and is
                # therefore not the value actually being enforced.
                "clamped": effective != configured,
            }
    return result


def resolve_integration_settings():
    """Return editable communication values and secret-free deployment status."""
    definitions, values = _scoped_values(INTEGRATION_FIELDS.values())
    deployment_name, deployment_address = _deployment_sender()
    fallbacks = {
        "email_sender_name": deployment_name,
        "email_sender_address": deployment_address,
        **INTEGRATION_DEFAULTS,
    }
    result = {"settings": {}, "sources": {}, "status": {}}
    for field, key in INTEGRATION_FIELDS.items():
        row = values.get(key)
        definition = definitions.get(key)
        if row is not None:
            value, source = row.value, "database"
        elif definition is not None and definition.default_value is not None:
            value, source = definition.default_value, "default"
        else:
            value = fallbacks[field]
            source = "environment" if field.startswith("email_sender_") else "default"
        result["settings"][field] = value
        result["sources"][field] = source

    result["status"] = {
        "email": {
            "configured": bool(getattr(settings, "EMAIL_HOST_USER", "")),
            "host": getattr(settings, "EMAIL_HOST", ""),
            "credentials_managed_by": "deployment",
        },
        "payments": {
            "provider": str(getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")).upper(),
            "configured": bool(
                getattr(settings, "PAYSTACK_SECRET_KEY", "")
                and getattr(settings, "PAYSTACK_PUBLIC_KEY", "")
            ),
            "credentials_managed_by": "deployment",
        },
        "public_application": {
            "base_url": getattr(settings, "FRONTEND_BASE_URL", ""),
            "managed_by": "deployment",
        },
    }
    return result


def validate_security_compliance(field, value, *, tenant=None, branch=None):
    """Reject a scoped value that would weaken its effective parent baseline."""
    if tenant is None:
        return
    policy = SECURITY_COMPLIANCE[field]
    parent = resolve_security_settings(tenant=tenant) if branch is not None else resolve_security_settings()
    boundary = parent["settings"][field]
    if policy["direction"] == "maximum" and value > boundary:
        raise ValueError(f"Must be {boundary} or lower to meet the parent security baseline.")
    if policy["direction"] == "minimum" and value < boundary:
        raise ValueError(f"Must be {boundary} or higher to meet the parent security baseline.")


def get_setting_consumer(key):
    return SETTING_CONSUMERS.get(key)


def get_security_settings(*, tenant=None, branch=None):
    """Read all security values once and fail safely to product defaults."""
    try:
        return resolve_security_settings(tenant=tenant, branch=branch)["settings"]
    except Exception:
        return dict(SECURITY_DEFAULTS)


def get_security_value(field, *, tenant=None, branch=None):
    """Read one security value and fail safely to the product default."""
    return get_security_settings(tenant=tenant, branch=branch)[field]


def get_integration_settings():
    """Read all integration values once and fail safely to their fallbacks."""
    try:
        return resolve_integration_settings()["settings"]
    except Exception:
        deployment_name, deployment_address = _deployment_sender()
        return {
            "email_sender_name": deployment_name,
            "email_sender_address": deployment_address,
            **INTEGRATION_DEFAULTS,
        }


def get_integration_value(field):
    """Read one integration value and fail safely to deployment/product defaults."""
    return get_integration_settings()[field]
