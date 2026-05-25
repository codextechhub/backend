"""
Management command: seed_import_templates
=========================================
Location: your_app/management/commands/seed_import_templates.py

Purpose
-------
Creates or updates the canonical ImportTemplate records and their
ImportTemplateColumn children for every supported dataset type.

This command is idempotent — running it multiple times is safe.
Existing templates are matched by `code` and updated in place.
Existing columns are matched by (template, column_name) and updated
in place. Nothing is deleted automatically; retired templates must be
manually retired via Django Admin or a separate command.

Usage
-----
    python manage.py seed_import_templates
    python manage.py seed_import_templates --dataset-type students
    python manage.py seed_import_templates --dry-run

Options
-------
    --dataset-type  Seed only one dataset type. Defaults to all.
    --dry-run       Print what would happen without writing to the database.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# ---------------------------------------------------------------------------
# Adjust this import path to match your actual app name
# ---------------------------------------------------------------------------
from vs_import_data.models import (
    ImportTemplate,
    ImportTemplateColumn,
    DatasetTypeChoices,
    FileFormatChoices,
    TemplateStatusChoices,
    TemplateColumnDataTypeChoices,
)


# ===========================================================================
# Template definitions
# ===========================================================================
# Each entry in TEMPLATES is a dict with two keys:
#   "template"  – kwargs passed directly to ImportTemplate (except `columns`)
#   "columns"   – list of dicts, each becoming one ImportTemplateColumn
#
# Rules:
#   - `code` must be globally unique and stable. Never change it once deployed.
#   - `column_name` must be unique within its template.
#   - `target_field` must be unique within its template.
#   - `column_order` controls the column sequence in generated files.
# ===========================================================================

TEMPLATES: list[dict] = [
    {
        # -----------------------------------------------------------------------
        # Schools
        # -----------------------------------------------------------------------
        # Covers: identity, ownership, academic config, and primary admin contact.
        # Branding (logo) is excluded — files cannot be uploaded via spreadsheet.
        # Package plan assignment is excluded — handled separately via platform UI.
        #
        # The slug column is the unique key used for cross-referencing in the
        # Branches template. Admins must define it carefully and consistently.
        # -----------------------------------------------------------------------
        "template": {
            "code": "schools_master_v1",
            "name": "Schools Master Import",
            "dataset_type": DatasetTypeChoices.SCHOOLS,
            "status": TemplateStatusChoices.ACTIVE,
            "default_file_format": FileFormatChoices.XLSX,
            "description": (
                "Template for bulk-creating School records on the platform. "
                "Each row defines one school (tenant). Import this before the "
                "Branches template, as branches reference the School Slug column."
            ),
            "instructions": (
                "Fill one school per row. Slug must be lowercase letters and "
                "hyphens only — no spaces or special characters. Example: greenfield-academy. "
                "Slugs cannot match reserved system words (admin, api, www, etc.). "
                "Date columns must follow the format YYYY-MM-DD. "
                "Import this file before the Branches template."
            ),
            "allow_sample_row": True,
            "sample_row_data": {
                "School Name":     "Greenfield Academy",
                "Slug":                 "greenfield-academy",
                "School Code":     "GFA",
                "Ownership Type":       "Private",
                "Address":              "14 Admiralty Way, Lekki Phase 1, Lagos",
                "Website":              "https://greenfieldacademy.edu.ng",
                "Motto":                "Excellence in Learning",
                "Term Structure":       "3 Terms",
                "Currency":             "NGN",
                "Registration ID":      "RC-2009-00234",
                "Admin Full Name":      "Mrs. Funke Adeyemi",
                "Admin Email":          "admin@greenfieldacademy.edu.ng",
                "Admin Phone":          "08051234567",
                "Admin Role":           "IT Head",
            },
            "validation_rules": {
                "allow_duplicate_slugs": False,
                "allow_duplicate_codes": False,
                "reserved_slugs": [
                    "admin", "api", "auth", "login", "logout", "www", "root",
                    "static", "media", "health", "status", "support",
                    "system", "internal",
                ],
            },
            "is_download_enabled": True,
        },
        "columns": [
            # --- Core identity ---
            {
                "column_name":   "School Name",
                "target_field":  "name",
                "display_name":  "School Name",
                "help_text":     "Full display name of the school. Example: Greenfield Academy.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   True,
                "is_unique":     False,
                "max_length":    255,
                "sample_value":  "Greenfield Academy",
                "column_order":  1,
            },
            {
                "column_name":   "Slug",
                "target_field":  "slug",
                "display_name":  "Slug",
                "help_text": (
                    "Unique URL-safe identifier. Lowercase letters, numbers, and hyphens only. "
                    "No spaces. Cannot be a reserved word. Example: greenfield-academy."
                ),
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   True,
                "is_unique":     True,
                "max_length":    80,
                "sample_value":  "greenfield-academy",
                "column_order":  2,
            },
            {
                "column_name":   "School Code",
                "target_field":  "code",
                "display_name":  "School Code",
                "help_text":     "Short alphanumeric code used in reports. Must be unique. Example: GFA.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     True,
                "max_length":    32,
                "sample_value":  "GFA",
                "column_order":  3,
            },
            {
                "column_name":   "Ownership Type",
                "target_field":  "ownership_type",
                "display_name":  "Ownership Type",
                "help_text":     "Operational classification of the school.",
                "data_type":     TemplateColumnDataTypeChoices.CHOICE,
                "is_required":   True,
                "is_unique":     False,
                "allowed_values": ["Public", "Private", "Faith-Based", "Non-Governmental Organization"],
                "sample_value":  "Private",
                "column_order":  4,
            },
    
            # --- Location and identity metadata ---
            {
                "column_name":   "Address",
                "target_field":  "address",
                "display_name":  "Address",
                "help_text":     "Summary address for the school's headquarters.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    255,
                "sample_value":  "14 Admiralty Way, Lekki Phase 1, Lagos",
                "column_order":  5,
            },
            {
                "column_name":   "Website",
                "target_field":  "website",
                "display_name":  "Website",
                "help_text":     "Full URL including https://. Leave blank if none.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    255,
                "sample_value":  "https://greenfieldacademy.edu.ng",
                "column_order":  6,
            },
            {
                "column_name":   "Motto",
                "target_field":  "motto",
                "display_name":  "Motto",
                "help_text":     "Optional school motto shown in onboarding and reports.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    255,
                "sample_value":  "Excellence in Learning",
                "column_order":  7,
            },
            {
                "column_name":   "Registration ID",
                "target_field":  "registration_id",
                "display_name":  "Registration ID",
                "help_text":     "Government or regulatory registration number. Example: RC-2009-00234.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    64,
                "sample_value":  "RC-2009-00234",
                "column_order":  8,
            },
    
            # --- Academic configuration ---
            {
                "column_name":   "Term Structure",
                "target_field":  "term_structure",
                "display_name":  "Term Structure",
                "help_text":     "Academic calendar format used by this school.",
                "data_type":     TemplateColumnDataTypeChoices.CHOICE,
                "is_required":   True,
                "is_unique":     False,
                "allowed_values": ["3 Terms", "2 Semesters"],
                "sample_value":  "3 Terms",
                "column_order":  9,
            },
            {
                "column_name":   "Currency",
                "target_field":  "currency",
                "display_name":  "Currency",
                "help_text":     "Preferred billing currency for this school.",
                "data_type":     TemplateColumnDataTypeChoices.CHOICE,
                "is_required":   True,
                "is_unique":     False,
                "allowed_values": ["NGN", "USD"],
                "sample_value":  "NGN",
                "column_order":  10,
            },
    
            # --- Primary admin contact ---
            # These columns create the ContactInfo and SchoolPrimaryAdmin
            # records during import. Prefixed with "Admin " to distinguish clearly
            # from any branch admin columns in other templates.
            {
                "column_name":   "Admin Full Name",
                "target_field":  "primary_admin_full_name",
                "display_name":  "Primary Admin Full Name",
                "help_text":     "Full name of the person who will be the school's primary admin.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   True,
                "is_unique":     False,
                "max_length":    120,
                "sample_value":  "Mrs. Funke Adeyemi",
                "column_order":  10,
            },
            {
                "column_name":   "Admin Email",
                "target_field":  "primary_admin_email",
                "display_name":  "Primary Admin Email",
                "help_text":     "Email address of the primary admin. An invite will be sent here.",
                "data_type":     TemplateColumnDataTypeChoices.EMAIL,
                "is_required":   True,
                "is_unique":     True,
                "sample_value":  "admin@greenfieldacademy.edu.ng",
                "column_order":  11,
            },
            {
                "column_name":   "Admin Phone",
                "target_field":  "primary_admin_phone",
                "display_name":  "Primary Admin Phone",
                "help_text":     "Phone number of the primary admin. Example: 08051234567.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    32,
                "sample_value":  "08051234567",
                "column_order":  12,
            },
            {
                "column_name":   "Admin Role",
                "target_field":  "primary_admin_role",
                "display_name":  "Primary Admin Role Title",
                "help_text": (
                    "Job title of the primary admin within the school. "
                    "Example: IT Head, School Director, Proprietor."
                ),
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    80,
                "sample_value":  "IT Head",
                "default_value": "IT Head",
                "column_order":  14,
            },
        ],
    },

    {
        # -----------------------------------------------------------------------
        # Branches
        # -----------------------------------------------------------------------
        # Covers: identity, type, contact, location, status, and branch admin.
        # School Slug cross-references an existing School row —
        # so the Schools template must be imported first.
        #
        # Branch codes are auto-allocated by Branch.save(), so no Code column
        # is included. Admins do not supply codes manually.
        # -----------------------------------------------------------------------
        "template": {
            "code": "branches_master_v1",
            "name": "Branches Master Import",
            "dataset_type": DatasetTypeChoices.BRANCHES,
            "status": TemplateStatusChoices.ACTIVE,
            "default_file_format": FileFormatChoices.XLSX,
            "description": (
                "Template for bulk-creating Branch (campus) records for existing schools. "
                "Each row defines one branch. The School Slug column must match a slug "
                "that already exists in the system. Import the Schools template first."
            ),
            "instructions": (
                "Fill one branch per row. School Slug must exactly match an existing "
                "school slug — check spelling carefully. "
                "Only one branch per school may have Is Main Branch set to TRUE. "
                "Branch codes are assigned automatically — do not add a code column. "
                "Date columns must follow YYYY-MM-DD format."
            ),
            "allow_sample_row": True,
            "sample_row_data": {
                "School Slug":     "greenfield-academy",
                "Branch Name":          "Lekki Campus",
                "Branch Type":          "Secondary",
                "Is Main Branch":       "TRUE",
                "Address":              "14 Admiralty Way, Lekki Phase 1, Lagos",
                "Email":                "lekki@greenfieldacademy.edu.ng",
                "Country":              "Nigeria",
                "State":                "Lagos",
                "Opened Date":          "2009-09-01",
                "Admin Full Name":      "Mr. Emeka Obi",
                "Admin Email":          "head.lekki@greenfieldacademy.edu.ng",
                "Admin Phone":          "08061234567",
                "Admin Role":           "Head Teacher",
            },
            "validation_rules": {
                "require_school_slug": True,
                "max_main_branches_per_school": 1,
                "auto_allocate_branch_code": True,
            },
            "is_download_enabled": True,
        },
        "columns": [
            # --- School linkage ---
            {
                "column_name":   "School Slug",
                "target_field":  "school_slug",
                "display_name":  "School Slug",
                "help_text": (
                    "Slug of the school this branch belongs to. "
                    "Must exactly match an existing school slug. Example: greenfield-academy."
                ),
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   True,
                "is_unique":     False,
                "max_length":    80,
                "sample_value":  "greenfield-academy",
                "reference_model":         "School",
                "reference_lookup_field":  "slug",
                "column_order":  1,
            },
    
            # --- Branch identity ---
            {
                "column_name":   "Branch Name",
                "target_field":  "name",
                "display_name":  "Branch Name",
                "help_text":     "Display name of the branch. Example: Lekki Campus, Ajah Campus.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   True,
                "is_unique":     False,
                "max_length":    255,
                "sample_value":  "Lekki Campus",
                "column_order":  2,
            },
            {
                "column_name":   "Branch Type",
                "target_field":  "_type",
                "display_name":  "Branch Type",
                "help_text": (
                    "Free-form descriptor for the branch level. "
                    "Common values: Primary, Secondary, Nursery, Tertiary, Mixed."
                ),
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    80,
                "sample_value":  "Secondary",
                "column_order":  3,
            },
            {
                "column_name":   "Is Main Branch",
                "target_field":  "is_main",
                "display_name":  "Is Main Branch",
                "help_text": (
                    "Set to TRUE for the primary campus. Only one branch per school "
                    "may be TRUE. All others must be FALSE."
                ),
                "data_type":     TemplateColumnDataTypeChoices.BOOLEAN,
                "is_required":   True,
                "is_unique":     False,
                "allowed_values": ["TRUE", "FALSE"],
                "sample_value":  "TRUE",
                "default_value": "FALSE",
                "column_order":  4,
            },
    
            # --- Contact and location ---
            {
                "column_name":   "Address",
                "target_field":  "address",
                "display_name":  "Branch Address",
                "help_text":     "Physical address of this campus.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    255,
                "sample_value":  "14 Admiralty Way, Lekki Phase 1, Lagos",
                "column_order":  5,
            },
            {
                "column_name":   "Email",
                "target_field":  "email",
                "display_name":  "Branch Email",
                "help_text":     "Contact email address for this branch.",
                "data_type":     TemplateColumnDataTypeChoices.EMAIL,
                "is_required":   False,
                "is_unique":     False,
                "sample_value":  "lekki@greenfieldacademy.edu.ng",
                "column_order":  6,
            },
            {
                "column_name":   "Country",
                "target_field":  "country",
                "display_name":  "Country",
                "help_text":     "Country where this branch is located. Default: Nigeria.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    80,
                "sample_value":  "Nigeria",
                "default_value": "Nigeria",
                "column_order":  7,
            },
            {
                "column_name":   "State",
                "target_field":  "state",
                "display_name":  "State",
                "help_text":     "State or province where this branch is located. Example: Lagos, FCT Abuja.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    120,
                "sample_value":  "Lagos",
                "column_order":  8,
            },
    
            # --- Lifecycle ---
            # Status is intentionally excluded: BranchCreateSerializer always
            # creates branches as PENDING and transitions via lifecycle methods.
            {
                "column_name":   "Opened Date",
                "target_field":  "opened_at",
                "display_name":  "Opened Date",
                "help_text":     "Date the branch was first opened. Format: YYYY-MM-DD. Leave blank if unknown.",
                "data_type":     TemplateColumnDataTypeChoices.DATE,
                "is_required":   False,
                "is_unique":     False,
                "sample_value":  "2009-09-01",
                "column_order":  9,
            },
    
            # --- Branch primary admin contact ---
            # These columns create the ContactInfo and BranchPrimaryAdmin records
            # during import. Prefixed "Admin " to distinguish from school admin.
            {
                "column_name":   "Admin Full Name",
                "target_field":  "branch_admin_full_name",
                "display_name":  "Branch Admin Full Name",
                "help_text":     "Full name of the person who will be this branch's primary admin.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   True,
                "is_unique":     False,
                "max_length":    120,
                "sample_value":  "Mr. Emeka Obi",
                "column_order":  10,
            },
            {
                "column_name":   "Admin Email",
                "target_field":  "branch_admin_email",
                "display_name":  "Branch Admin Email",
                "help_text":     "Email address of the branch admin. An invite will be queued.",
                "data_type":     TemplateColumnDataTypeChoices.EMAIL,
                "is_required":   True,
                "is_unique":     True,
                "sample_value":  "head.lekki@greenfieldacademy.edu.ng",
                "column_order":  11,
            },
            {
                "column_name":   "Admin Phone",
                "target_field":  "branch_admin_phone",
                "display_name":  "Branch Admin Phone",
                "help_text":     "Phone number of the branch admin. Example: 08061234567.",
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    32,
                "sample_value":  "08061234567",
                "column_order":  12,
            },
            {
                "column_name":   "Admin Role",
                "target_field":  "branch_admin_role",
                "display_name":  "Branch Admin Role Title",
                "help_text": (
                    "Job title of the branch admin within this campus. "
                    "Example: Head Teacher, Campus Director, Principal."
                ),
                "data_type":     TemplateColumnDataTypeChoices.STRING,
                "is_required":   False,
                "is_unique":     False,
                "max_length":    80,
                "sample_value":  "Head Teacher",
                "default_value": "Head Teacher",
                "column_order":  13,
            },
        ],
    }
]


# ===========================================================================
# Helper: build a lookup index from TEMPLATES by dataset_type
# ===========================================================================
TEMPLATES_BY_DATASET_TYPE: dict[str, list[dict]] = {}
for _entry in TEMPLATES:
    _dt = _entry["template"]["dataset_type"]
    TEMPLATES_BY_DATASET_TYPE.setdefault(_dt, []).append(_entry)


# ===========================================================================
# Command
# ===========================================================================
class Command(BaseCommand):
    help = "Seed canonical ImportTemplate and ImportTemplateColumn records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset-type",
            type=str,
            choices=[c[0] for c in DatasetTypeChoices.choices],
            default=None,
            help="Seed only this dataset type. Omit to seed all.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be created/updated without writing to the database.",
        )

    def handle(self, *args, **options):
        dataset_type_filter: str | None = options["dataset_type"]
        dry_run: bool = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be saved.\n"))

        # Filter down to the requested dataset type if one was given
        entries_to_process = (
            TEMPLATES_BY_DATASET_TYPE.get(dataset_type_filter, [])
            if dataset_type_filter
            else TEMPLATES
        )

        if not entries_to_process:
            raise CommandError(
                f"No templates defined for dataset type '{dataset_type_filter}'."
            )

        templates_created = 0
        templates_updated = 0
        columns_created = 0
        columns_updated = 0

        for entry in entries_to_process:
            template_data: dict = entry["template"]
            columns_data: list[dict] = entry["columns"]
            code = template_data["code"]

            self.stdout.write(f"\nProcessing template: {code}")

            if dry_run:
                exists = ImportTemplate.objects.filter(code=code).exists()
                action = "UPDATE" if exists else "CREATE"
                self.stdout.write(f"  [{action}] ImportTemplate → {code}")
                for col in columns_data:
                    self.stdout.write(f"    [UPSERT] Column → {col['column_name']}")
                continue

            # ------------------------------------------------------------------
            # Wrap each template + its columns in a transaction so a column
            # failure does not leave a half-seeded template behind.
            # ------------------------------------------------------------------
            with transaction.atomic():
                template, created = ImportTemplate.objects.update_or_create(
                    code=code,
                    defaults={
                        **{k: v for k, v in template_data.items() if k != "code"},
                        # Mark published_at the first time an ACTIVE template is seeded
                        **(
                            {"published_at": timezone.now()}
                            if template_data.get("status") == TemplateStatusChoices.ACTIVE
                            else {}
                        ),
                    },
                )

                if created:
                    templates_created += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"  [CREATED] ImportTemplate → {code}")
                    )
                else:
                    templates_updated += 1
                    self.stdout.write(f"  [UPDATED] ImportTemplate → {code}")

                # Upsert each column
                for col_data in columns_data:
                    column_name = col_data["column_name"]
                    col, col_created = ImportTemplateColumn.objects.update_or_create(
                        template=template,
                        column_name=column_name,
                        defaults={
                            k: v for k, v in col_data.items() if k != "column_name"
                        },
                    )

                    if col_created:
                        columns_created += 1
                        self.stdout.write(
                            self.style.SUCCESS(f"    [CREATED] Column → {column_name}")
                        )
                    else:
                        columns_updated += 1
                        self.stdout.write(f"    [UPDATED] Column → {column_name}")

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        self.stdout.write("\n" + "=" * 50)
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run complete. Nothing was saved."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. "
                    f"Templates: {templates_created} created, {templates_updated} updated. "
                    f"Columns: {columns_created} created, {columns_updated} updated."
                )
            )