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
        "platform.entitlements.enforce", "Enforce Plan Entitlements",
        "Whether a request is refused when the school's plan does not reach "
        "the capability behind the permission being used. Off by default and "
        "readable per school, so enforcement arrives one school at a time "
        "rather than for the whole platform on a deploy.",
        "BOOLEAN", False, {},
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

#: The modules a school is sold. Every school is granted every one of them;
#: the plan it pays for decides how far into each it reaches. Nothing here is
#: withheld, which is why a school can see a module it has not paid to use in
#: full and ask for more rather than waiting to be sold it.
#:
#: (key, label, requires_entitlement)
MODULES = [
    ("students", "Students Management", True),
    ("teachers", "Teachers Management", True),
    ("parents", "Parents Management", True),
    ("attendance", "Attendance Management", True),
    ("finance", "Finance", True),
    ("procurement", "Procurement", True),
    ("gradebook", "Gradebook and Assessments", True),
    ("calendar", "Calendar and Timetable", True),
    ("student_portal", "Student Portal", True),
    ("parent_portal", "Parent and Guardian Portal", True),
    # The cross-cutting services, held as one module so that a school reaches
    # bulk import everywhere at once rather than module by module. A bursar who
    # imported 900 students in January and is refused a 300-line vendor list in
    # March cannot see why the same button stopped working.
    ("platform", "Platform Services", True),
]

#: What each depth is called on the price list, per module. Three bands for
#: every module, the same three everywhere, because the whole argument for
#: selling depth rests on Plus meaning one thing.
DEPTH_BANDS = [
    ("CORE", "Core", "Every school, every tier"),
    ("PLUS", "Plus", "Standard and above"),
    ("ADVANCED", "Advanced", "Premium and above"),
]

#: Bands that carry their own key because something already refers to them by
#: name - a permission map, a runtime check, or a line on the price list that
#: is sold as itself rather than as "the Plus of that module".
#:
#: (key, label, module key, depth)
NAMED_BANDS = [
    # Core, not Plus. Loading a roll from a spreadsheet is how a school arrives
    # rather than something it grows into: a new school on the shallowest plan
    # has four hundred students in a file and no other way in, and pricing the
    # only door above them makes the cheapest plan the hardest one to start on.
    # It also sits on the onboarding checklist, so putting it out of reach put
    # a step there that most new schools could not take.
    ("bulk_import", "Bulk Data Import", "platform", "CORE"),
    ("data_export", "Data Export and Reporting", "platform", "PLUS"),
    # sms_alerts was removed 2026-07-12 - SMS is not part of the product.
    # Existing rows were archived (is_active=False), not deleted.
    ("email_alerts", "Email Notification Alerts", "platform", "CORE"),
]

DEPENDENCIES = {
    "procurement": ["finance"],
    "parent_portal": ["student_portal"],
}

#: Capabilities that no longer describe anything sellable. Archived rather than
#: deleted, the way sms_alerts was: entitlements, overrides and audit history
#: point at these rows.
#:
#: ``vendors`` was a module, then briefly a Core band of Procurement. Vendor
#: work is the shallow end of procurement rather than a separate purchase, and
#: a second Core band beside ``procurement_core`` was a distinction nothing
#: could act on: a school reaching one always reached the other.
#:
#: An empty band is not by itself a reason to retire one. Attendance, Gradebook
#: and the portals have empty bands because nobody has built them, and those
#: are the price list's shape for what is coming. The bands here are different:
#: their keys live in named siblings at the same depth, so they would stay
#: empty however much gets built.
RETIRED = [
    "vendors",
    # The platform module's keys all sit in named bands - email alerts at
    # Core, bulk import and data export at Plus - so its generic bands hold
    # nothing and structurally never will. Two rows at one depth of one module
    # cannot be told apart, and a school's plan page listed "Core, Core".
    "platform_core",
    "platform_plus",
    "platform_advanced",
]


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
        modules = {}
        for key, label, requires_entitlement in MODULES:
            modules[key], _ = Capability.objects.update_or_create(
                key=key,
                defaults={
                    "label": label, "kind": Capability.Kind.MODULE,
                    "parent": None, "depth": None,
                    "requires_entitlement": requires_entitlement, "is_active": True,
                },
            )
        bands = {}
        for module_key, module in modules.items():
            for depth_name, depth_label, description in DEPTH_BANDS:
                band_key = f"{module_key}_{depth_name.lower()}"
                bands[band_key], _ = Capability.objects.update_or_create(
                    key=band_key,
                    defaults={
                        "label": f"{module.label}: {depth_label}",
                        "description": description,
                        "kind": Capability.Kind.FEATURE,
                        "parent": module,
                        "depth": Capability.Depth[depth_name],
                        # A band holds no grant of its own; its module's depth
                        # is what opens it.
                        "requires_entitlement": False,
                        "is_active": True,
                    },
                )
        for key, label, module_key, depth_name in NAMED_BANDS:
            bands[key], _ = Capability.objects.update_or_create(
                key=key,
                defaults={
                    "label": label, "kind": Capability.Kind.FEATURE,
                    "parent": modules[module_key],
                    "depth": Capability.Depth[depth_name],
                    "requires_entitlement": False, "is_active": True,
                },
            )
        retired = Capability.objects.filter(key__in=RETIRED, is_active=True)
        retired_count = retired.update(is_active=False)
        for key, requirements in DEPENDENCIES.items():
            for required_key in requirements:
                CapabilityDependency.objects.get_or_create(
                    capability=modules[key], requires=modules[required_key]
                )
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(modules)} modules and {len(bands)} bands."
        ))
        if retired_count:
            self.stdout.write(self.style.WARNING(
                f"Archived {retired_count} retired capability row(s): "
                f"{', '.join(RETIRED)}."
            ))
