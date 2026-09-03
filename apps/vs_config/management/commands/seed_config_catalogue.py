from django.core.management.base import BaseCommand
from django.db import transaction

from vs_config.models import Capability, CapabilityDependency, ConfigurationDefinition


# (key, label, description, value_type, default_value, validation_rules)
DEFINITIONS = [
    (
        "notifications.email_max_retries", "Email Delivery Max Retries",
        "Maximum delivery attempts for a queued email notification.",
        "INTEGER", 3, {"min": 0, "max": 10},
    ),
    (
        "notifications.email_retry_backoff_seconds", "Email Retry Backoff Seconds",
        "Base backoff in seconds between email delivery retries.",
        "INTEGER", 60, {"min": 1, "max": 3600},
    ),
    (
        "security.failed_login_threshold", "Failed Login Threshold",
        "Failed password attempts allowed before the account is locked.",
        "INTEGER", 5, {"min": 3, "max": 20},
    ),
    (
        "security.account_lock_minutes", "Account Lock Duration",
        "Minutes an account remains locked after reaching the failed-login threshold.",
        "INTEGER", 15, {"min": 5, "max": 1440},
    ),
    (
        "security.self_reset_expiry_hours", "Self-service Reset Lifetime",
        "Hours a self-service password reset link remains valid.",
        "INTEGER", 1, {"min": 1, "max": 24},
    ),
    (
        "security.admin_reset_expiry_hours", "Admin Reset Lifetime",
        "Hours an administrator-triggered password reset link remains valid.",
        "INTEGER", 24, {"min": 1, "max": 168},
    ),
    (
        "security.invitation_expiry_days", "Invitation Lifetime",
        "Days a new-user invitation remains valid, including after resend.",
        "INTEGER", 7, {"min": 1, "max": 30},
    ),
    (
        "security.proxy_idle_timeout_minutes", "Proxy Session Idle Timeout",
        "Minutes an open-ended proxy session may remain idle before it expires.",
        "INTEGER", 30, {"min": 5, "max": 120},
    ),
    (
        "integrations.email.sender_name", "Default Email Sender Name",
        "Display name used for platform email when a message does not provide its own sender name.",
        "STRING", None, {},
    ),
    (
        "integrations.email.sender_address", "Default Email Sender Address",
        "From address used for platform email. SMTP credentials remain deployment-owned.",
        "STRING", None, {},
    ),
    (
        "platform.profile.name", "Platform Name",
        "Name printed when the platform is the issuer of a Finance document.",
        "STRING", None, {},
    ),
    (
        "platform.profile.tagline", "Platform Tagline",
        "Short line printed below the platform name on issued documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.address", "Platform Address",
        "Contact address printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.email", "Platform Email",
        "Contact email printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.phone", "Platform Phone",
        "Contact phone printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.website", "Platform Website",
        "Website printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.logo_url", "Platform Logo URL",
        "Public logo URL used on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.onboarding.default_ownership_type", "Default School Ownership",
        "Ownership type preselected when a new school omits the field.",
        "CHOICE", "PUBLIC", {"choices": ["PUBLIC", "PRIVATE", "FAITH_BASED", "NGO"]},
    ),
    (
        "platform.onboarding.default_term_structure", "Default Academic Structure",
        "Academic calendar structure used when a new school omits the field.",
        "CHOICE", "3_TERMS", {"choices": ["2_SEMESTERS", "3_TERMS"]},
    ),
    (
        "platform.onboarding.default_currency", "Default School Currency",
        "Billing currency used when a new school omits the field.",
        "CHOICE", "NGN", {"choices": ["NGN", "USD"]},
    ),
    (
        "platform.onboarding.default_branch_country", "Default Branch Country",
        "Country used when a new branch omits the field.",
        "STRING", "Nigeria", {},
    ),
]

# (key, label, description, value_type, default_value, validation_rules)
#
# Settings a SCHOOL may override. Held in their own table rather than behind a
# seventh tuple element, so the platform-only list above cannot acquire a
# school scope through a typo in a column nobody reads.
SCHOOL_SCOPED_DEFINITIONS = [
    (
        "students.admission_number.required", "Admission Number Required",
        "Whether every student at this school must be given an admission "
        "number when they are enrolled.",
        "BOOLEAN", False, {},
    ),
    (
        "students.admission_number.pattern", "Admission Number Pattern",
        "A regular expression every admission number at this school must "
        "match. Anchored by the server, so it cannot match part of a longer "
        "number. Empty means any shape is accepted.",
        "STRING", "", {},
    ),
    (
        "students.admission_number.hint", "Admission Number Hint",
        "The sentence shown under the admission number field, and quoted "
        "verbatim when a number is refused. This is the only one of the three "
        "a person reads, which is why a refusal never quotes the pattern.",
        "STRING", "", {},
    ),
]

CAPABILITIES = [
    ("students", "Students Management", "MODULE", True),
    ("teachers", "Teachers Management", "MODULE", True),
    ("parents", "Parents Management", "MODULE", True),
    ("attendance", "Attendance Management", "MODULE", True),
    ("finance", "Finance", "MODULE", True),
    ("procurement", "Procurement", "MODULE", True),
    ("vendors", "Vendors Management", "MODULE", True),
    ("gradebook", "Gradebook and Assessments", "MODULE", True),
    ("student_portal", "Student Portal", "MODULE", True),
    ("parent_portal", "Parent and Guardian Portal", "MODULE", True),
    ("bulk_import", "Bulk Data Import", "FEATURE", False),
    ("data_export", "Data Export and Reporting", "FEATURE", False),
    # sms_alerts was removed 2026-07-12 - SMS is not part of the product.
    # Existing rows were archived (is_active=False), not deleted.
    ("email_alerts", "Email Notification Alerts", "FEATURE", False),
]
DEPENDENCIES = {
    "procurement": ["finance"],
    "parent_portal": ["student_portal"],
}


class Command(BaseCommand):
    help = "Seed the capability catalogue and platform configuration definitions."

    @transaction.atomic
    def handle(self, *args, **options):
        for key, label, description, value_type, default, rules in DEFINITIONS:
            ConfigurationDefinition.objects.get_or_create(
                key=key,
                defaults={
                    "label": label, "description": description,
                    "value_type": value_type, "default_value": default,
                    "validation_rules": rules, "allowed_scopes": ["platform"],
                },
            )
        for key, label, description, value_type, default, rules in SCHOOL_SCOPED_DEFINITIONS:
            row, created = ConfigurationDefinition.objects.get_or_create(
                key=key,
                defaults={
                    "label": label, "description": description,
                    "value_type": value_type, "default_value": default,
                    "validation_rules": rules,
                    "allowed_scopes": ["platform", "school"],
                },
            )
            # get_or_create leaves an existing row alone, so widen it here:
            # a platform-only row refuses every school write.
            if not created and "school" not in (row.allowed_scopes or []):
                row.allowed_scopes = sorted({*(row.allowed_scopes or []), "platform", "school"})
                row.save(update_fields=["allowed_scopes"])
        rows = {}
        for key, label, kind, requires_entitlement in CAPABILITIES:
            rows[key], _ = Capability.objects.update_or_create(
                key=key,
                defaults={
                    "label": label, "kind": kind,
                    "requires_entitlement": requires_entitlement, "is_active": True,
                },
            )
        for key, requirements in DEPENDENCIES.items():
            for required_key in requirements:
                CapabilityDependency.objects.get_or_create(
                    capability=rows[key], requires=rows[required_key]
                )
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(rows)} capabilities."))
