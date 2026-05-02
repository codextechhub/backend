# vs_config/constants.py
#
# Central constants for the System Configuration & Feature Flags module.
#
# Design rules:
#   - FLAG_REGISTRY is the single source of truth for valid flag keys.
#     New flags are added here (code deployment), never via database rows.
#   - FLAG_DEPENDENCY_MAP defines which flags must be enabled before another
#     flag can be turned on, and which flags block disabling another.
#   - PERMITTED_SELF_SERVICE_KEYS defines the exact set of keys an Institution
#     Admin is allowed to read and update for their own institution.
#   - PERMISSION_KEYS align with the platform-wide module.resource.action format
#     established in vs_rbac.


# ---------------------------------------------------------------------------
# Feature flag registry
# key   → human-readable label shown in Vision Admin Console
# ---------------------------------------------------------------------------
FLAG_REGISTRY = {
    # Module access flags
    "modules.finance":         "Finance & Billing Module",
    "modules.procurement":     "Procurement & Vendor Module",
    "modules.attendance":      "Attendance Management Module",
    "modules.gradebook":       "Gradebook & Assessments Module",
    "modules.student_portal":  "Student Portal",
    "modules.parent_portal":   "Parent / Guardian Portal",

    # Feature-level flags
    "features.bulk_import":    "Bulk Data Import Engine",
    "features.data_export":    "Data Export & Reporting",
    "features.sms_alerts":     "SMS Notification Alerts",
    "features.email_alerts":   "Email Notification Alerts",
}

# ---------------------------------------------------------------------------
# Flag dependency map
# key   → list of flag keys that must be enabled BEFORE this flag can be on.
# Enforcement is bidirectional:
#   - Enabling key requires all values to already be enabled.
#   - Disabling a value is blocked if key is currently enabled.
# ---------------------------------------------------------------------------
FLAG_DEPENDENCY_MAP = {
    "modules.procurement":  ["modules.finance"],
    "modules.parent_portal": ["modules.student_portal"],
    "features.sms_alerts":  ["modules.finance"],   # requires billing to be active
}

# ---------------------------------------------------------------------------
# Branch Admin self-service override keys
# Only these keys may be read or written by Branch Admins.
# All other config keys are Vision-staff-only.
# ---------------------------------------------------------------------------
PERMITTED_SELF_SERVICE_KEYS = [
    "branch.timezone",         # IANA timezone string, e.g. Africa/Lagos
    "branch.locale",           # locale string, e.g. en-NG
    "branch.date_format",      # display format string, e.g. DD/MM/YYYY
    "branch.currency_display", # display label, e.g. NGN or ₦
]

# Valid date format tokens (allowed values for institution.date_format)
VALID_DATE_FORMATS = [
    "DD/MM/YYYY",
    "MM/DD/YYYY",
    "YYYY-MM-DD",
    "DD-MM-YYYY",
    "D MMM YYYY",
]

# ---------------------------------------------------------------------------
# ConfigurationChangeLog change_type choices
# ---------------------------------------------------------------------------
class ChangeType:
    GLOBAL_CONFIG    = "GLOBAL_CONFIG"
    FEATURE_FLAG     = "FEATURE_FLAG"
    BRANCH_OVERRIDE  = "BRANCH_OVERRIDE"

    CHOICES = [
        (GLOBAL_CONFIG,   "Global Config"),
        (FEATURE_FLAG,    "Feature Flag"),
        (BRANCH_OVERRIDE, "Branch Override"),
    ]

# ---------------------------------------------------------------------------
# Permission keys — module.resource.action format (vs_rbac pattern)
# ---------------------------------------------------------------------------
class ConfigPermissions:
    # Vision Super Admin — full system config access
    SYSTEM_MANAGE    = "config.system.manage"

    # Support Officer — feature flag management only
    FLAGS_MANAGE     = "config.flags.manage"

    # Branch Admin — self-service override access
    SELF_MANAGE      = "config.branch.self_manage"

    ALL = [
        SYSTEM_MANAGE,
        FLAGS_MANAGE,
        SELF_MANAGE,
    ]
