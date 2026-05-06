from django.core.management.base import BaseCommand

from vs_rbac.models import Permission, PrebuiltRolePermission, PrebuiltRoleTemplate


SUGGESTIONS = [
    {"key": "institution_admin",     "name": "Institution Admin",              "scope": "institution", "tier": "A", "description": "Full institution-wide administration. Commonly held by the Proprietor or Director."},
    {"key": "principal",             "name": "Principal",                      "scope": "branch",       "tier": "A", "description": "Academic and operational head of a branch."},
    {"key": "branch_admin",          "name": "Branch Admin",                   "scope": "branch",       "tier": "A", "description": "Administrative manager of a single branch."},
    {"key": "class_teacher",         "name": "Class Teacher",                  "scope": "class",        "tier": "A", "description": "Homeroom teacher responsible for a single class. Known as Form Teacher (NG/GH/KE), Homeroom Teacher (US)."},
    {"key": "registrar",             "name": "Registrar",                      "scope": "branch",       "tier": "A", "description": "Manages student enrolment, records, and guardian data."},
    {"key": "finance_admin",         "name": "Finance Admin",                  "scope": "branch",       "tier": "A", "description": "Full finance administration. Commonly held by the Bursar."},
    {"key": "read_only_viewer",      "name": "Read-Only Viewer",               "scope": "branch",       "tier": "A", "description": "View-only access across all modules."},
    {"key": "vp_academics",          "name": "Vice Principal (Academics)",     "scope": "branch",       "tier": "C", "description": "Oversees academic structure, curriculum, and assessment."},
    {"key": "vp_administration",     "name": "Vice Principal (Administration)","scope": "branch",       "tier": "C", "description": "Oversees staff and operational administration."},
    {"key": "subject_teacher",       "name": "Subject Teacher",                "scope": "class",        "tier": "C", "description": "Teaches one or more subjects across classes."},
    {"key": "head_of_department",    "name": "Head of Department",             "scope": "branch",       "tier": "C", "description": "Leads an academic department. Commonly held by the HOD."},
    {"key": "examination_officer",   "name": "Examination Officer",            "scope": "branch",       "tier": "B", "description": "Manages gradebook and assessment records."},
    {"key": "guidance_counsellor",   "name": "Guidance Counsellor",           "scope": "branch",       "tier": "C", "description": "Student support and welfare."},
    {"key": "data_entry_officer",    "name": "Data Entry Officer",             "scope": "branch",       "tier": "C", "description": "Enters and manages data records."},
    {"key": "finance_officer",       "name": "Finance Officer",                "scope": "branch",       "tier": "B", "description": "Handles day-to-day payment recording and receipts."},
    {"key": "billing_officer",       "name": "Billing Officer",                "scope": "branch",       "tier": "B", "description": "Generates invoices and manages fee billing."},
    {"key": "procurement_admin",     "name": "Procurement Admin",              "scope": "branch",       "tier": "B", "description": "Full procurement, vendor, and inventory administration."},
    {"key": "store_keeper",          "name": "Store Keeper",                   "scope": "branch",       "tier": "B", "description": "Manages inventory records."},
    {"key": "procurement_requester", "name": "Procurement Requester",          "scope": "branch",       "tier": "B", "description": "Creates and tracks procurement requests."},
    {"key": "nurse_medical_officer", "name": "Nurse / Medical Officer",        "scope": "branch",       "tier": "C", "description": "Manages student medical notes and health records."},
    {"key": "sports_coordinator",    "name": "Sports Coordinator",             "scope": "branch",       "tier": "C", "description": "Manages sports programmes and activities."},
    {"key": "librarian",             "name": "Librarian",                      "scope": "branch",       "tier": "C", "description": "Manages library resources and access."},
    {"key": "ict_administrator",     "name": "ICT Administrator",              "scope": "branch",       "tier": "C", "description": "Manages user accounts and system configuration."},
    {"key": "report_viewer",         "name": "Report Viewer",                  "scope": "branch",       "tier": "B", "description": "View-only access to reports."},
    {"key": "parent_guardian",       "name": "Parent / Guardian",              "scope": "portal",       "tier": "A", "description": "Parent or guardian portal access."},
    {"key": "student",               "name": "Student",                        "scope": "portal",       "tier": "A", "description": "Student portal access."},
]


class Command(BaseCommand):
    help = "Seed PrebuiltRoleTemplate records with their default permissions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print actions without writing.")
        parser.add_argument("--reset", action="store_true", help="Delete all prebuilt roles and re-seed. Dev only.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        reset = options["reset"]

        if reset:
            if dry_run:
                self.stdout.write(self.style.WARNING("--reset ignored in --dry-run mode."))
            else:
                deleted_perms, _ = PrebuiltRolePermission.objects.all().delete()
                deleted_roles, _ = PrebuiltRoleTemplate.objects.all().delete()
                self.stdout.write(self.style.WARNING(f"Reset: deleted {deleted_roles} suggestions, {deleted_perms} permissions."))

        created_roles = 0
        updated_roles = 0
        created_perms = 0

        for data in SUGGESTIONS:
            key = data["key"]
            self.stdout.write(f"  Processing: {key}")

            if dry_run:
                self.stdout.write(f"    [dry-run] Would upsert PrebuiltRoleTemplate key={key}")
            else:
                obj, created = PrebuiltRoleTemplate.objects.update_or_create(
                    key=key,
                    defaults={
                        "name": data["name"],
                        "description": data.get("description", ""),
                        "scope": data["scope"],
                        "tier": data["tier"],
                        "is_active": True,
                    },
                )
                if created:
                    created_roles += 1
                else:
                    updated_roles += 1

            perms = self._resolve_permissions(key)
            self.stdout.write(f"    {len(perms)} permissions resolved for {key}")

            if not dry_run and perms:
                existing = set(
                    PrebuiltRolePermission.objects.filter(prebuilt_role=obj)
                    .values_list("permission_id", flat=True)
                )
                new_records = [
                    PrebuiltRolePermission(prebuilt_role=obj, permission=p)
                    for p in perms
                    if p.key not in existing
                ]
                if new_records:
                    PrebuiltRolePermission.objects.bulk_create(new_records, ignore_conflicts=True)
                    created_perms += len(new_records)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Roles created={created_roles} updated={updated_roles} "
            f"permission links created={created_perms}"
        ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_perm(self, key):
        try:
            return Permission.objects.get(key=key)
        except Permission.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"  Permission not found: {key} — skipping"))
            return None

    def _get_perms_by_prefix(self, prefix):
        return list(Permission.objects.filter(key__startswith=prefix, is_active=True))

    def _resolve_permissions(self, key):
        perms = []

        if key == "institution_admin":
            for prefix in [
                "students", "staff", "academic_structure", "academic_calendar",
                "attendance", "gradebook", "assessments", "billing", "payments",
                "finance_ledger", "discounts", "refunds", "procurement", "vendors",
                "purchase_orders", "inventory", "dashboards", "reports", "notifications",
                "onboarding", "data_import",
            ]:
                perms.extend(self._get_perms_by_prefix(prefix))

        elif key in ("principal", "branch_admin"):
            for prefix in [
                "students", "staff", "academic_structure", "academic_calendar",
                "attendance", "gradebook", "assessments", "dashboards", "reports",
            ]:
                perms.extend(self._get_perms_by_prefix(prefix))
            for k in [
                "billing.invoice.view", "payments.view",
                "finance_ledger.reports.view", "procurement.request.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "class_teacher":
            for k in [
                "students.view_class", "attendance.record", "attendance.view",
                "gradebook.scores.enter", "gradebook.scores.view", "timetable.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "registrar":
            for k in [
                "students.enrol", "students.manage_branch", "students.view_class",
                "guardian.manage", "student_documents.upload",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "finance_admin":
            for prefix in ["billing", "payments", "finance_ledger", "discounts", "refunds"]:
                perms.extend(self._get_perms_by_prefix(prefix))

        elif key == "finance_officer":
            for k in [
                "payments.record", "payments.receipt.generate",
                "payments.view", "billing.invoice.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "billing_officer":
            for k in [
                "billing.invoice.generate", "billing.invoice.view",
                "billing.fee_structure.view", "payments.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "procurement_admin":
            for prefix in ["procurement", "vendors", "purchase_orders", "inventory"]:
                perms.extend(self._get_perms_by_prefix(prefix))

        elif key == "store_keeper":
            perms.extend(self._get_perms_by_prefix("inventory"))

        elif key == "procurement_requester":
            for k in ["procurement.request.create", "procurement.request.view"]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "examination_officer":
            for prefix in ["gradebook", "assessments"]:
                perms.extend(self._get_perms_by_prefix(prefix))

        elif key == "head_of_department":
            for k in [
                "gradebook.scores.view", "assessments.view", "students.view_class",
                "procurement.request.create", "procurement.request.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "vp_academics":
            for prefix in ["academic_structure", "academic_calendar", "gradebook", "assessments"]:
                perms.extend(self._get_perms_by_prefix(prefix))
            p = self._get_perm("attendance.view")
            if p:
                perms.append(p)

        elif key == "vp_administration":
            for prefix in ["staff", "attendance"]:
                perms.extend(self._get_perms_by_prefix(prefix))
            for k in ["procurement.request.create", "procurement.request.view"]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "guidance_counsellor":
            for k in [
                "students.view_class", "students.medical_notes.view", "attendance.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "nurse_medical_officer":
            for k in [
                "students.medical_notes.view", "students.medical_notes.edit", "attendance.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "ict_administrator":
            for k in [
                "users.account.create", "users.account.deactivate",
                "users.account.view", "system_config.view",
            ]:
                p = self._get_perm(k)
                if p:
                    perms.append(p)

        elif key == "read_only_viewer":
            from django.db.models import Q
            perms = list(Permission.objects.filter(
                Q(key__endswith=".view") | Q(key__endswith=".list"),
                is_active=True,
            ))

        elif key == "report_viewer":
            perms.extend(self._get_perms_by_prefix("reports"))

        elif key == "parent_guardian":
            perms.extend(self._get_perms_by_prefix("parent_portal"))

        elif key == "student":
            perms.extend(self._get_perms_by_prefix("student_portal"))

        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for p in perms:
            if p.key not in seen:
                seen.add(p.key)
                unique.append(p)
        return unique
