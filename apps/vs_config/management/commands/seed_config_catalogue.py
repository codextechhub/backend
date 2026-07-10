from django.core.management.base import BaseCommand
from django.db import transaction

from vs_config.models import Capability, CapabilityDependency


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
    ("sms_alerts", "SMS Notification Alerts", "FEATURE", False),
    ("email_alerts", "Email Notification Alerts", "FEATURE", False),
]
DEPENDENCIES = {
    "procurement": ["finance"],
    "parent_portal": ["student_portal"],
    "sms_alerts": ["finance"],
}


class Command(BaseCommand):
    help = "Seed the unified module and feature capability catalogue."

    @transaction.atomic
    def handle(self, *args, **options):
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
