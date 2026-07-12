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
    # sms_alerts was removed 2026-07-12 — SMS is not part of the product.
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
