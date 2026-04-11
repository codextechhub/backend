"""
Management command: seed_missing_permissions
============================================
Adds ONLY the permission keys that are referenced by school RoleTemplates
and PlatformRoleTemplates but are NOT present in the main seed_permissions
command (which covers Modules 1-27 / 505 keys).

These are purpose-built shorthand keys that map to school-facing UI
capabilities. They intentionally use a different namespace convention
(e.g.  students.*  vs the engine-level  student.*) so there is zero
collision risk with the existing permission registry.

Groups added
------------
  dashboard.*         3 keys  — school dashboard widgets
  students.*         12 keys  — school-scoped student management actions
  staff.*            10 keys  — school-scoped staff management actions
  academics.*        11 keys  — curriculum, timetable, lessons
  assessments.*      10 keys  — scores, results, exams, report cards
  attendance.*        5 keys  — school-facing attendance actions
  finance.*          15 keys  — school-scoped fee / invoice / budget actions
  library.*           6 keys  — library operations
  health.*            4 keys  — sick bay / medical
  communication.*     5 keys  — school messaging shortcuts
  admissions.*        4 keys  — admissions pipeline
  hostel.*            5 keys  — boarding / hostel
  transport.*         4 keys  — transport management
  canteen.*           4 keys  — cafeteria / canteen
  events.*            3 keys  — school events calendar
  alumni.*            3 keys  — alumni network
  settings.*          7 keys  — school configuration
  reports.*           2 keys  — school-wide reports
  audit.*             2 keys  — school-scoped audit access
  platform.*         41 keys  — Vision-internal platform capabilities

Total: 156 new keys

Usage
-----
    python manage.py seed_missing_permissions
    python manage.py seed_missing_permissions --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

# ---------------------------------------------------------------------------
# Format: (key, module_key, action, description, sensitivity, is_restricted)
# ---------------------------------------------------------------------------

MISSING_PERMISSIONS: list[tuple] = [

    # ══════════════════════════════════════════════════════════════════════
    # SCHOOL-DOMAIN PERMISSIONS
    # ══════════════════════════════════════════════════════════════════════

    # ── DASHBOARD ──────────────────────────────────────────────────────────
    ("dashboard.overview.view",         "dashboard",    "view",     "View the main school dashboard overview panel.",                   "NORMAL",    False),
    ("dashboard.analytics.view",        "dashboard",    "view",     "View analytics and summary widgets on the dashboard.",             "NORMAL",    False),
    ("dashboard.announcements.manage",  "dashboard",    "manage",   "Create, edit and publish school-wide announcements.",              "NORMAL",    False),

    # ── STUDENTS ───────────────────────────────────────────────────────────
    ("students.profile.view",           "students",     "view",     "View student profiles within the school tenant.",                  "NORMAL",    False),
    ("students.profile.create",         "students",     "create",   "Enrol a new student and create their profile record.",             "NORMAL",    False),
    ("students.profile.update",         "students",     "update",   "Edit student profile details (name, DOB, contact, etc.).",         "NORMAL",    False),
    ("students.profile.export",         "students",     "export",   "Export student profile records to CSV or XLSX.",                   "SENSITIVE", True),
    ("students.class.assign",           "students",     "assign",   "Assign a student to a class or arm.",                             "NORMAL",    False),
    ("students.class.transfer",         "students",     "transfer", "Transfer a student between classes or branches.",                  "SENSITIVE", True),
    ("students.disciplinary.view",      "students",     "view",     "View a student's disciplinary history and notes.",                 "SENSITIVE", False),
    ("students.disciplinary.manage",    "students",     "manage",   "Add or update disciplinary records for a student.",                "SENSITIVE", False),
    ("students.medical.view",           "students",     "view",     "View student medical and health records.",                         "CRITICAL",  True),
    ("students.guardian.view",          "students",     "view",     "View linked parent or guardian records for a student.",            "NORMAL",    False),
    ("students.guardian.manage",        "students",     "manage",   "Link, edit or remove parent/guardian records.",                    "NORMAL",    False),
    ("students.id_card.generate",       "students",     "generate", "Generate and print student ID cards.",                             "NORMAL",    False),

    # ── STAFF ──────────────────────────────────────────────────────────────
    ("staff.profile.view",              "staff",        "view",     "View staff member profiles within the school.",                    "NORMAL",    False),
    ("staff.profile.create",            "staff",        "create",   "Onboard a new staff member and create their account.",             "SENSITIVE", False),
    ("staff.profile.update",            "staff",        "update",   "Edit staff member profile and contact details.",                   "SENSITIVE", False),
    ("staff.profile.export",            "staff",        "export",   "Export staff records to CSV or XLSX.",                             "SENSITIVE", True),
    ("staff.salary.view",               "staff",        "view",     "View staff salary and payroll information.",                       "CRITICAL",  True),
    ("staff.leave.approve",             "staff",        "approve",  "Approve or reject staff leave requests.",                          "SENSITIVE", False),
    ("staff.appraisal.view",            "staff",        "view",     "View staff performance appraisal records.",                        "SENSITIVE", False),
    ("staff.appraisal.manage",          "staff",        "manage",   "Conduct, update and submit staff appraisal reports.",              "SENSITIVE", False),
    ("staff.attendance.view",           "staff",        "view",     "View staff attendance and punctuality records.",                   "NORMAL",    False),
    ("staff.attendance.mark",           "staff",        "mark",     "Mark or edit daily staff attendance.",                             "NORMAL",    False),

    # ── ACADEMICS ──────────────────────────────────────────────────────────
    ("academics.curriculum.view",       "academics",    "view",     "View the school curriculum and scheme of work.",                   "NORMAL",    False),
    ("academics.curriculum.manage",     "academics",    "manage",   "Create and edit curriculum content and learning objectives.",      "SENSITIVE", False),
    ("academics.timetable.view",        "academics",    "view",     "View published class timetables.",                                 "NORMAL",    False),
    ("academics.timetable.manage",      "academics",    "manage",   "Create, edit and publish timetable schedules.",                    "NORMAL",    False),
    ("academics.subjects.view",         "academics",    "view",     "View the school subject catalogue.",                               "NORMAL",    False),
    ("academics.subjects.manage",       "academics",    "manage",   "Add, edit subjects and assign them to teachers.",                  "NORMAL",    False),
    ("academics.class.view",            "academics",    "view",     "View class and arm listings.",                                     "NORMAL",    False),
    ("academics.class.manage",          "academics",    "manage",   "Create and manage classes and arms.",                              "NORMAL",    False),
    ("academics.lesson_notes.view",     "academics",    "view",     "View uploaded lesson notes and plans.",                            "NORMAL",    False),
    ("academics.lesson_notes.manage",   "academics",    "manage",   "Upload and edit lesson notes for assigned subjects.",              "NORMAL",    False),
    ("academics.lesson_notes.approve",  "academics",    "approve",  "Approve lesson notes before they are published to students.",     "NORMAL",    False),

    # ── ASSESSMENTS & RESULTS ──────────────────────────────────────────────
    ("assessments.scores.view",         "assessments",  "view",     "View student assessment and examination scores.",                  "NORMAL",    False),
    ("assessments.scores.enter",        "assessments",  "enter",    "Enter CA or test scores for assigned subjects.",                   "NORMAL",    False),
    ("assessments.scores.edit",         "assessments",  "edit",     "Edit previously submitted score entries.",                         "SENSITIVE", True),
    ("assessments.scores.approve",      "assessments",  "approve",  "Approve and ratify submitted score sheets.",                       "SENSITIVE", False),
    ("assessments.results.publish",     "assessments",  "publish",  "Publish term or semester results to students and parents.",        "SENSITIVE", False),
    ("assessments.results.export",      "assessments",  "export",   "Export result sheets and grade reports.",                          "SENSITIVE", True),
    ("assessments.report_card.print",   "assessments",  "print",    "Generate and print student report cards.",                         "NORMAL",    False),
    ("assessments.exam_schedule.view",  "assessments",  "view",     "View the published examination timetable.",                        "NORMAL",    False),
    ("assessments.exam_schedule.manage","assessments",  "manage",   "Create and publish examination timetables.",                       "SENSITIVE", False),
    ("assessments.grading.manage",      "assessments",  "manage",   "Configure grading scales, boundaries and promotion rules.",       "SENSITIVE", True),

    # ── ATTENDANCE ─────────────────────────────────────────────────────────
    ("attendance.student.view",         "attendance",   "view",     "View student attendance records and summaries.",                   "NORMAL",    False),
    ("attendance.student.mark",         "attendance",   "mark",     "Mark daily or period-level student attendance.",                   "NORMAL",    False),
    ("attendance.student.edit",         "attendance",   "edit",     "Edit previously marked attendance entries.",                       "SENSITIVE", True),
    ("attendance.student.export",       "attendance",   "export",   "Export student attendance data reports.",                          "SENSITIVE", True),
    ("attendance.student.report",       "attendance",   "report",   "Generate attendance summary and anomaly reports.",                 "NORMAL",    False),

    # ── FINANCE (school-scoped shorthand) ──────────────────────────────────
    # Note: these sit in the top-level "finance" namespace, distinct from
    # finance.billing.*, finance.payment.*, finance.ledger.* engine keys.
    ("finance.fees.view",               "finance",      "view",     "View school fee schedules and student fee records.",               "SENSITIVE", False),
    ("finance.fees.manage",             "finance",      "manage",   "Create and update school fee structures.",                         "CRITICAL",  True),
    ("finance.fees.waive",              "finance",      "waive",    "Grant full or partial fee waivers to students.",                   "CRITICAL",  True),
    ("finance.invoice.view",            "finance",      "view",     "View student fee invoices.",                                       "SENSITIVE", False),
    ("finance.invoice.create",          "finance",      "create",   "Generate invoices for individual students.",                       "SENSITIVE", False),
    ("finance.invoice.approve",         "finance",      "approve",  "Approve invoices before they are sent to parents.",               "CRITICAL",  True),
    ("finance.payment.record",          "finance",      "record",   "Record manual cash or bank payment receipts.",                     "SENSITIVE", False),
    ("finance.payment.verify",          "finance",      "verify",   "Verify uploaded proof-of-payment documents.",                      "SENSITIVE", False),
    ("finance.payment.reverse",         "finance",      "reverse",  "Reverse or void an incorrectly recorded payment.",                "CRITICAL",  True),
    ("finance.expenditure.view",        "finance",      "view",     "View school expenditure and petty cash records.",                  "CRITICAL",  True),
    ("finance.expenditure.manage",      "finance",      "manage",   "Record and approve school expenditure entries.",                   "CRITICAL",  True),
    ("finance.reports.view",            "finance",      "view",     "View financial summary and income/expense reports.",               "CRITICAL",  True),
    ("finance.reports.export",          "finance",      "export",   "Export financial reports to PDF or XLSX.",                         "CRITICAL",  True),
    ("finance.budget.view",             "finance",      "view",     "View the school's annual budget allocations.",                     "CRITICAL",  True),
    ("finance.budget.manage",           "finance",      "manage",   "Create, edit and approve school budget line items.",               "CRITICAL",  True),

    # ── LIBRARY ────────────────────────────────────────────────────────────
    ("library.catalog.view",            "library",      "view",     "View the school library book catalogue.",                          "NORMAL",    False),
    ("library.catalog.manage",          "library",      "manage",   "Add, edit and remove books in the catalogue.",                     "NORMAL",    False),
    ("library.borrow.record",           "library",      "record",   "Issue books to students or staff borrowers.",                      "NORMAL",    False),
    ("library.borrow.return",           "library",      "return",   "Process the return of borrowed library books.",                    "NORMAL",    False),
    ("library.overdue.manage",          "library",      "manage",   "Manage overdue book follow-ups and fines.",                        "NORMAL",    False),
    ("library.reports.view",            "library",      "view",     "View library usage and circulation reports.",                      "NORMAL",    False),

    # ── HEALTH ─────────────────────────────────────────────────────────────
    ("health.visits.view",              "health",       "view",     "View student sick-bay visit logs.",                                "SENSITIVE", False),
    ("health.visits.record",            "health",       "record",   "Log a student sick-bay or medical visit.",                         "SENSITIVE", False),
    ("health.medication.manage",        "health",       "manage",   "Manage medication stock and administration records.",              "CRITICAL",  True),
    ("health.reports.view",             "health",       "view",     "View school health and medical summary reports.",                  "CRITICAL",  True),

    # ── COMMUNICATION (school shorthand) ───────────────────────────────────
    ("communication.sms.send",          "communication","send",     "Send SMS notifications to parents or staff.",                      "NORMAL",    False),
    ("communication.email.send",        "communication","send",     "Send broadcast emails to school stakeholders.",                    "NORMAL",    False),
    ("communication.announcement.post", "communication","post",     "Post announcements on the school notice board.",                   "NORMAL",    False),
    ("communication.chat.view",         "communication","view",     "View internal school messaging threads.",                          "NORMAL",    False),
    ("communication.chat.send",         "communication","send",     "Send messages in internal school chat.",                           "NORMAL",    False),

    # ── ADMISSIONS ─────────────────────────────────────────────────────────
    ("admissions.application.view",     "admissions",   "view",     "View incoming admission applications.",                            "NORMAL",    False),
    ("admissions.application.process",  "admissions",   "process",  "Shortlist applicants and schedule interviews.",                    "SENSITIVE", False),
    ("admissions.application.approve",  "admissions",   "approve",  "Formally approve or reject admission applications.",               "SENSITIVE", True),
    ("admissions.enrollment.confirm",   "admissions",   "confirm",  "Convert an accepted applicant to an enrolled student.",            "SENSITIVE", False),

    # ── HOSTEL / BOARDING ──────────────────────────────────────────────────
    ("hostel.room.view",                "hostel",       "view",     "View hostel room listings and student allocations.",               "NORMAL",    False),
    ("hostel.room.manage",              "hostel",       "manage",   "Create rooms and allocate students to hostel rooms.",              "NORMAL",    False),
    ("hostel.attendance.mark",          "hostel",       "mark",     "Conduct hostel roll call and record attendance.",                  "NORMAL",    False),
    ("hostel.incident.report",          "hostel",       "report",   "Log a hostel or boarding incident.",                               "SENSITIVE", False),
    ("hostel.incident.manage",          "hostel",       "manage",   "Review, resolve and escalate hostel incidents.",                   "SENSITIVE", False),

    # ── TRANSPORT ──────────────────────────────────────────────────────────
    ("transport.routes.view",           "transport",    "view",     "View school bus routes and vehicle assignments.",                  "NORMAL",    False),
    ("transport.routes.manage",         "transport",    "manage",   "Create and edit transport routes and vehicle records.",            "NORMAL",    False),
    ("transport.students.assign",       "transport",    "assign",   "Assign students to transport routes.",                             "NORMAL",    False),
    ("transport.tracking.view",         "transport",    "view",     "View live GPS or vehicle tracking data.",                          "NORMAL",    False),

    # ── CANTEEN ────────────────────────────────────────────────────────────
    ("canteen.menu.view",               "canteen",      "view",     "View the canteen menu and pricing.",                               "NORMAL",    False),
    ("canteen.menu.manage",             "canteen",      "manage",   "Update canteen menu items and prices.",                            "NORMAL",    False),
    ("canteen.orders.manage",           "canteen",      "manage",   "Process and fulfil canteen orders.",                               "NORMAL",    False),
    ("canteen.sales.report",            "canteen",      "report",   "Generate canteen daily and weekly sales reports.",                 "NORMAL",    False),

    # ── EVENTS ─────────────────────────────────────────────────────────────
    ("events.calendar.view",            "events",       "view",     "View the school events calendar.",                                 "NORMAL",    False),
    ("events.calendar.manage",          "events",       "manage",   "Create and edit events on the school calendar.",                   "NORMAL",    False),
    ("events.attendance.track",         "events",       "track",    "Record attendance at school events.",                              "NORMAL",    False),

    # ── ALUMNI ─────────────────────────────────────────────────────────────
    ("alumni.profile.view",             "alumni",       "view",     "View the alumni directory and profiles.",                          "NORMAL",    False),
    ("alumni.profile.manage",           "alumni",       "manage",   "Update alumni profile records.",                                   "NORMAL",    False),
    ("alumni.communications.send",      "alumni",       "send",     "Send communications and newsletters to alumni.",                   "NORMAL",    False),

    # ── SETTINGS ───────────────────────────────────────────────────────────
    ("settings.school.view",            "settings",     "view",     "View school-wide configuration settings.",                         "NORMAL",    False),
    ("settings.school.manage",          "settings",     "manage",   "Edit school-wide configuration and branding.",                     "CRITICAL",  True),
    ("settings.branch.view",            "settings",     "view",     "View branch-level configuration settings.",                        "NORMAL",    False),
    ("settings.branch.manage",          "settings",     "manage",   "Edit branch-level configuration.",                                 "SENSITIVE", True),
    ("settings.academic_session.manage","settings",     "manage",   "Open, close and roll over academic sessions and terms.",           "CRITICAL",  True),
    ("settings.roles.view",             "settings",     "view",     "View role and permission configurations for the school.",          "SENSITIVE", False),
    ("settings.roles.manage",           "settings",     "manage",   "Create and edit school roles and assign permissions.",             "CRITICAL",  True),

    # ── REPORTS (school-wide) ──────────────────────────────────────────────
    ("reports.school_wide.view",        "reports",      "view",     "Access consolidated school-wide reports.",                         "SENSITIVE", False),
    ("reports.school_wide.export",      "reports",      "export",   "Export consolidated school-wide reports.",                         "SENSITIVE", True),

    # ── AUDIT (school-scoped shorthand) ────────────────────────────────────
    ("audit.logs.view",                 "audit",        "view",     "View the school-scoped system audit trail.",                       "CRITICAL",  True),
    ("audit.logs.export",               "audit",        "export",   "Export school-scoped audit log records.",                          "CRITICAL",  True),

    # ══════════════════════════════════════════════════════════════════════
    # PLATFORM PERMISSIONS (Vision-internal)
    # ══════════════════════════════════════════════════════════════════════

    # ── SCHOOL MANAGEMENT ─────────────────────────────────────────────────
    ("platform.schools.view",           "platform",     "view",     "View all schools registered on the XVS platform.",                "SENSITIVE", False),
    ("platform.schools.create",         "platform",     "create",   "Onboard a new school onto the XVS platform.",                     "CRITICAL",  True),
    ("platform.schools.update",         "platform",     "update",   "Update any school's profile and configuration.",                   "CRITICAL",  True),
    ("platform.schools.suspend",        "platform",     "suspend",  "Suspend or reactivate a school account platform-wide.",            "CRITICAL",  True),
    ("platform.schools.delete",         "platform",     "delete",   "Permanently delete a school and all its data.",                    "CRITICAL",  True),

    # ── BILLING / SUBSCRIPTIONS ───────────────────────────────────────────
    ("platform.billing.view",           "platform",     "view",     "View school billing records and subscription plans.",              "CRITICAL",  True),
    ("platform.billing.manage",         "platform",     "manage",   "Edit plans, issue credits and manage platform invoices.",          "CRITICAL",  True),
    ("platform.billing.export",         "platform",     "export",   "Export platform billing and subscription data.",                   "CRITICAL",  True),

    # ── USER MANAGEMENT ───────────────────────────────────────────────────
    ("platform.users.view",             "platform",     "view",     "View all platform and school-level user accounts.",                "SENSITIVE", False),
    ("platform.users.impersonate",      "platform",     "impersonate","Impersonate a school user for audited support diagnostics.",     "CRITICAL",  True),
    ("platform.users.suspend",          "platform",     "suspend",  "Suspend or reactivate any user account platform-wide.",            "CRITICAL",  True),
    ("platform.users.delete",           "platform",     "delete",   "Permanently delete a user account.",                              "CRITICAL",  True),

    # ── ROLES & PERMISSIONS ───────────────────────────────────────────────
    ("platform.roles.view",             "platform",     "view",     "View platform role templates and their permission sets.",          "SENSITIVE", False),
    ("platform.roles.manage",           "platform",     "manage",   "Create and edit platform-level role templates.",                   "CRITICAL",  True),
    ("platform.permissions.manage",     "platform",     "manage",   "Add or remove entries in the global permission registry.",         "CRITICAL",  True),

    # ── SUPPORT ───────────────────────────────────────────────────────────
    ("platform.support.tickets.view",   "platform",     "view",     "View support tickets submitted by all schools.",                   "SENSITIVE", False),
    ("platform.support.tickets.manage", "platform",     "manage",   "Respond to and resolve school support tickets.",                   "SENSITIVE", False),
    ("platform.support.escalate",       "platform",     "escalate", "Escalate a support ticket to engineering or leadership.",          "SENSITIVE", False),

    # ── ANALYTICS & REPORTING ─────────────────────────────────────────────
    ("platform.analytics.view",         "platform",     "view",     "View platform-wide analytics dashboards.",                         "SENSITIVE", False),
    ("platform.analytics.export",       "platform",     "export",   "Export platform-wide analytics datasets.",                         "CRITICAL",  True),
    ("platform.reports.financial",      "platform",     "view",     "View consolidated platform financial reports.",                    "CRITICAL",  True),

    # ── SYSTEM / INFRASTRUCTURE ───────────────────────────────────────────
    ("platform.system.config.view",     "platform",     "view",     "View platform-level system configuration values.",                 "CRITICAL",  True),
    ("platform.system.config.manage",   "platform",     "manage",   "Edit platform-level system settings and config.",                  "CRITICAL",  True),
    ("platform.system.deployments.view","platform",     "view",     "View deployment history, release notes and rollout status.",       "SENSITIVE", False),
    ("platform.system.deployments.trigger","platform",  "trigger",  "Trigger production deployments or initiate rollbacks.",            "CRITICAL",  True),
    ("platform.system.logs.view",       "platform",     "view",     "View application and server-side system logs.",                    "CRITICAL",  True),
    ("platform.system.maintenance.manage","platform",   "manage",   "Enable or disable global maintenance mode.",                       "CRITICAL",  True),

    # ── INTEGRATIONS ──────────────────────────────────────────────────────
    ("platform.integrations.view",      "platform",     "view",     "View third-party integration configurations.",                     "SENSITIVE", False),
    ("platform.integrations.manage",    "platform",     "manage",   "Add, edit and revoke platform-level integrations.",                "CRITICAL",  True),

    # ── COMPLIANCE & AUDIT ────────────────────────────────────────────────
    ("platform.compliance.view",        "platform",     "view",     "View compliance frameworks, checklists and evidence.",             "CRITICAL",  True),
    ("platform.compliance.manage",      "platform",     "manage",   "Create and update compliance records and evidence packs.",         "CRITICAL",  True),
    ("platform.audit.logs.view",        "platform",     "view",     "View the platform-wide immutable audit log.",                      "CRITICAL",  True),
    ("platform.audit.logs.export",      "platform",     "export",   "Export platform-wide audit log records.",                          "CRITICAL",  True),

    # ── DATA ENGINEERING ──────────────────────────────────────────────────
    ("platform.data.pipelines.view",    "platform",     "view",     "View data pipeline definitions and run statuses.",                 "SENSITIVE", False),
    ("platform.data.pipelines.manage",  "platform",     "manage",   "Create, edit and trigger data pipeline runs.",                     "CRITICAL",  True),
    ("platform.data.migrations.run",    "platform",     "run",      "Execute database schema and data migrations.",                     "CRITICAL",  True),
    ("platform.data.backups.view",      "platform",     "view",     "View backup schedules and available restore points.",              "CRITICAL",  True),
    ("platform.data.backups.manage",    "platform",     "manage",   "Create, schedule and restore from database backups.",              "CRITICAL",  True),

    # ── SECURITY ──────────────────────────────────────────────────────────
    ("platform.security.incidents.view","platform",     "view",     "View security incident records and investigation notes.",          "CRITICAL",  True),
    ("platform.security.incidents.manage","platform",   "manage",   "Investigate, update and resolve security incidents.",              "CRITICAL",  True),
    ("platform.security.pen_test.manage","platform",    "manage",   "Manage penetration test plans, scopes and findings.",              "CRITICAL",  True),
]


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Idempotently seeds the 156 permission keys needed by school "
        "RoleTemplates and PlatformRoleTemplates that are absent from "
        "the main seed_permissions command."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without touching the database.",
        )

    def handle(self, *args, **options):
        from vs_rbac.models import Permission  # noqa: lazy import

        dry_run: bool = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes.\n"))

        existing_keys: set[str] = set(
            Permission.objects.values_list("key", flat=True)
        )

        to_create = [p for p in MISSING_PERMISSIONS if p[0] not in existing_keys]
        already   = len(MISSING_PERMISSIONS) - len(to_create)

        self.stdout.write(
            f"Permissions defined : {len(MISSING_PERMISSIONS)}\n"
            f"Already in DB       : {already}\n"
            f"To be created       : {len(to_create)}\n"
        )

        if dry_run:
            for key, *_ in to_create:
                self.stdout.write(f"  [DRY RUN] would create → {key}")
            self.stdout.write(self.style.WARNING("\nDry run complete. No writes made."))
            return

        created = 0
        with transaction.atomic():
            for key, module_key, action, description, sensitivity, is_restricted in to_create:
                _, was_created = Permission.objects.get_or_create(
                    key=key,
                    defaults=dict(
                        module_key=module_key,
                        action=action,
                        description=description,
                        sensitivity_level=sensitivity,
                        is_restricted=is_restricted,
                        is_active=True,
                    ),
                )
                if was_created:
                    created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✅  Done. {created} new permissions created, "
                f"{already} already existed (skipped)."
            )
        )
        self.stdout.write(
            "\nNext step → run: python manage.py seed_roles_and_permissions"
        )