"""
Management command: seed_roles_and_permissions
===============================================
Single authoritative seeder for the full RBAC permission registry, role
templates, and permission groups. Supersedes the former seed_missing_perms
command — all school-domain and platform-domain permission keys are now
declared here.

Idempotently seeds:

  1. Permission registry               → Permission          (161 keys)
  2. Permission dependencies           → PermissionDependency
  3. Permission groups (reusable)      → PermissionGroup + GroupPermission
  4. School role templates             → SchoolRoleTemplate        (school-scoped)
  5. School role↔permission direct map → SchoolRolePermission      (residuals only)
  6. School role↔group attachments     → SchoolRoleGroup
  7. Platform role templates           → PlatformRoleTemplate (Vision-owned)
  8. Platform role↔permission direct   → PlatformRolePermission (residuals)
  9. Platform role↔group attachments   → PlatformRoleGroup

Permission namespaces
---------------------
  dashboard.*      students.*     staff.*        academics.*    assessments.*
  attendance.*     finance.*      library.*      health.*       communication.*
  admissions.*     hostel.*       transport.*    canteen.*      events.*
  alumni.*         settings.*     reports.*      audit.*        platform.*

Run
---
    python manage.py seed_roles_and_permissions
    python manage.py seed_roles_and_permissions --dry-run
    python manage.py seed_roles_and_permissions --school-slug demo-primary

Notes
-----
* Every write uses update_or_create so the command is safe to re-run.
* --dry-run prints a summary without touching the database.
* Direct SchoolRolePermission rows are only created for permissions NOT already
  covered by an attached group (residuals), so the effective permission set
  equals the explicitly declared ``permissions`` list for every role.
* Platform roles are global; they are always seeded regardless of school.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# ---------------------------------------------------------------------------
# 1. PERMISSION REGISTRY
#    Format: (key, module_key, action, description, sensitivity, is_restricted)
# ---------------------------------------------------------------------------

PERMISSIONS: list[tuple] = [

    # ── DASHBOARD ──────────────────────────────────────────────────────────
    ("dashboard.overview.view",         "dashboard",    "view",     "View the main school dashboard overview panel.",                     "NORMAL",    False),    ("dashboard.analytics.view",        "dashboard",    "view",     "View analytics and summary widgets on the dashboard.",              "NORMAL",    False),    ("dashboard.announcements.manage",  "dashboard",    "manage",   "Create, edit and publish school-wide announcements.",          "NORMAL",    False),
    # ── STUDENTS ───────────────────────────────────────────────────────────
    ("students.profile.view",           "students",     "view",     "View student profiles within the school tenant.",                            "NORMAL",    False),    ("students.profile.create",         "students",     "create",   "Enrol a new student and create their profile record.",                              "NORMAL",    False),    ("students.profile.update",         "students",     "update",   "Edit student profile details (name, DOB, contact, etc.).",                     "NORMAL",    False),    ("students.profile.delete",         "students",     "delete",   "Remove a student record permanently",              "CRITICAL",  True),
    ("students.profile.export",         "students",     "export",   "Export student profile records to CSV or XLSX.",               "SENSITIVE", True),    ("students.class.assign",           "students",     "assign",   "Assign a student to a class or arm.",                    "NORMAL",    False),    ("students.class.transfer",         "students",     "transfer", "Transfer a student between classes or branches.",     "SENSITIVE", True),    ("students.disciplinary.view",      "students",     "view",     "View a student's disciplinary history and notes.",                        "SENSITIVE", False),    ("students.disciplinary.manage",    "students",     "manage",   "Add or update disciplinary records for a student.",                    "SENSITIVE", False),    ("students.medical.view",           "students",     "view",     "View student medical and health records.",                     "CRITICAL",  True),    ("students.medical.manage",         "students",     "manage",   "Update student medical/health records",            "CRITICAL",  True),
    ("students.guardian.view",          "students",     "view",     "View linked parent or guardian records for a student.",              "NORMAL",    False),    ("students.guardian.manage",        "students",     "manage",   "Link, edit or remove parent/guardian records.",                  "NORMAL",    False),    ("students.id_card.generate",       "students",     "generate", "Generate and print student ID cards.",              "NORMAL",    False),
    # ── STAFF ──────────────────────────────────────────────────────────────
    ("staff.profile.view",              "staff",        "view",     "View staff member profiles within the school.",                              "NORMAL",    False),    ("staff.profile.create",            "staff",        "create",   "Onboard a new staff member and create their account.",                       "SENSITIVE", False),    ("staff.profile.update",            "staff",        "update",   "Edit staff member profile and contact details.",                       "SENSITIVE", False),    ("staff.profile.delete",            "staff",        "delete",   "Remove a staff record",                            "CRITICAL",  True),
    ("staff.profile.export",            "staff",        "export",   "Export staff records to CSV or XLSX.",                             "SENSITIVE", True),    ("staff.salary.view",               "staff",        "view",     "View staff salary and payroll information.",                      "CRITICAL",  True),    ("staff.salary.manage",             "staff",        "manage",   "Create or modify payroll entries",                 "CRITICAL",  True),
    ("staff.leave.apply",               "staff",        "apply",    "Submit a leave request",                           "NORMAL",    False),
    ("staff.leave.approve",             "staff",        "approve",  "Approve or reject staff leave requests.",           "SENSITIVE", False),    ("staff.appraisal.view",            "staff",        "view",     "View staff performance appraisal records.",                     "SENSITIVE", False),    ("staff.appraisal.manage",          "staff",        "manage",   "Conduct, update and submit staff appraisal reports.",              "SENSITIVE", False),    ("staff.attendance.view",           "staff",        "view",     "View staff attendance and punctuality records.",                    "NORMAL",    False),    ("staff.attendance.mark",           "staff",        "mark",     "Mark or edit daily staff attendance.",                    "NORMAL",    False),
    # ── ACADEMICS ──────────────────────────────────────────────────────────
    ("academics.curriculum.view",       "academics",    "view",     "View the school curriculum and scheme of work.",               "NORMAL",    False),    ("academics.curriculum.manage",     "academics",    "manage",   "Create and edit curriculum content and learning objectives.",                       "SENSITIVE", False),    ("academics.timetable.view",        "academics",    "view",     "View published class timetables.",                            "NORMAL",    False),    ("academics.timetable.manage",      "academics",    "manage",   "Create, edit and publish timetable schedules.",                    "NORMAL",    False),    ("academics.subjects.view",         "academics",    "view",     "View the school subject catalogue.",                           "NORMAL",    False),    ("academics.subjects.manage",       "academics",    "manage",   "Add, edit subjects and assign them to teachers.",            "NORMAL",    False),    ("academics.class.view",            "academics",    "view",     "View class and arm listings.",                      "NORMAL",    False),    ("academics.class.manage",          "academics",    "manage",   "Create and manage classes and arms.",                   "NORMAL",    False),    ("academics.lesson_notes.view",     "academics",    "view",     "View uploaded lesson notes and plans.",                          "NORMAL",    False),    ("academics.lesson_notes.manage",   "academics",    "manage",   "Upload and edit lesson notes for assigned subjects.",                      "NORMAL",    False),    ("academics.lesson_notes.approve",  "academics",    "approve",  "Approve lesson notes before they are published to students.",           "NORMAL",    False),
    # ── ASSESSMENTS & RESULTS ──────────────────────────────────────────────
    ("assessments.scores.view",         "assessments",  "view",     "View student assessment and examination scores.",                   "NORMAL",    False),    ("assessments.scores.enter",        "assessments",  "enter",    "Enter CA or test scores for assigned subjects.",       "NORMAL",    False),    ("assessments.scores.edit",         "assessments",  "edit",     "Edit previously submitted score entries.",                 "SENSITIVE", True),    ("assessments.scores.approve",      "assessments",  "approve",  "Approve and ratify submitted score sheets.",                      "SENSITIVE", False),    ("assessments.results.publish",     "assessments",  "publish",  "Publish term or semester results to students and parents.",        "SENSITIVE", False),    ("assessments.results.export",      "assessments",  "export",   "Export result sheets and grade reports.",                             "SENSITIVE", True),    ("assessments.report_card.print",   "assessments",  "print",    "Generate and print student report cards.",                       "NORMAL",    False),    ("assessments.exam_schedule.view",  "assessments",  "view",     "View the published examination timetable.",                       "NORMAL",    False),    ("assessments.exam_schedule.manage","assessments",  "manage",   "Create and publish examination timetables.",               "SENSITIVE", False),    ("assessments.grading.manage",      "assessments",  "manage",   "Configure grading scales, boundaries and promotion rules.",     "SENSITIVE", True),
    # ── ATTENDANCE ─────────────────────────────────────────────────────────
    ("attendance.student.view",         "attendance",   "view",     "View student attendance records and summaries.",                  "NORMAL",    False),    ("attendance.student.mark",         "attendance",   "mark",     "Mark daily or period-level student attendance.",        "NORMAL",    False),    ("attendance.student.edit",         "attendance",   "edit",     "Edit previously marked attendance entries.",                "SENSITIVE", True),    ("attendance.student.export",       "attendance",   "export",   "Export student attendance data reports.",                        "SENSITIVE", True),    ("attendance.student.report",       "attendance",   "report",   "Generate attendance summary and anomaly reports.",              "NORMAL",    False),
    # ── FINANCE ────────────────────────────────────────────────────────────
    ("finance.fees.view",               "finance",      "view",     "View school fee schedules and student fee records.",       "SENSITIVE", False),    ("finance.fees.manage",             "finance",      "manage",   "Create and update school fee structures.",                 "CRITICAL",  True),    ("finance.fees.waive",              "finance",      "waive",    "Grant full or partial fee waivers to students.",                "CRITICAL",  True),    ("finance.invoice.view",            "finance",      "view",     "View student fee invoices.",                            "SENSITIVE", False),    ("finance.invoice.create",          "finance",      "create",   "Generate invoices for individual students.",                   "SENSITIVE", False),    ("finance.invoice.approve",         "finance",      "approve",  "Approve invoices before they are sent to parents.",                  "CRITICAL",  True),    ("finance.payment.record",          "finance",      "record",   "Record manual cash or bank payment receipts.",                   "SENSITIVE", False),    ("finance.payment.verify",          "finance",      "verify",   "Verify uploaded proof-of-payment documents.",             "SENSITIVE", False),    ("finance.payment.reverse",         "finance",      "reverse",  "Reverse or void an incorrectly recorded payment.",                 "CRITICAL",  True),    ("finance.expenditure.view",        "finance",      "view",     "View school expenditure and petty cash records.",                  "CRITICAL",  True),    ("finance.expenditure.manage",      "finance",      "manage",   "Record and approve school expenditure entries.",           "CRITICAL",  True),    ("finance.reports.view",            "finance",      "view",     "View financial summary and income/expense reports.",                   "CRITICAL",  True),    ("finance.reports.export",          "finance",      "export",   "Export financial reports to PDF or XLSX.",                         "CRITICAL",  True),    ("finance.budget.view",             "finance",      "view",     "View the school's annual budget allocations.",                        "CRITICAL",  True),    ("finance.budget.manage",           "finance",      "manage",   "Create, edit and approve school budget line items.",                  "CRITICAL",  True),
    # ── LIBRARY ────────────────────────────────────────────────────────────
    ("library.catalog.view",            "library",      "view",     "View the school library book catalogue.",                      "NORMAL",    False),    ("library.catalog.manage",          "library",      "manage",   "Add, edit and remove books in the catalogue.",               "NORMAL",    False),    ("library.borrow.record",           "library",      "record",   "Issue books to students or staff borrowers.",                         "NORMAL",    False),    ("library.borrow.return",           "library",      "return",   "Process the return of borrowed library books.",                             "NORMAL",    False),    ("library.overdue.manage",          "library",      "manage",   "Manage overdue book follow-ups and fines.",                   "NORMAL",    False),    ("library.reports.view",            "library",      "view",     "View library usage and circulation reports.",                       "NORMAL",    False),
    # ── HEALTH / MEDICAL ───────────────────────────────────────────────────
    ("health.visits.view",              "health",       "view",     "View student sick-bay visit logs.",                 "SENSITIVE", False),    ("health.visits.record",            "health",       "record",   "Log a student sick-bay or medical visit.",                     "SENSITIVE", False),    ("health.medication.manage",        "health",       "manage",   "Manage medication stock and administration records.",  "CRITICAL",  True),    ("health.reports.view",             "health",       "view",     "View school health and medical summary reports.",              "CRITICAL",  True),
    # ── COMMUNICATION ──────────────────────────────────────────────────────
    ("communication.sms.send",          "communication","send",     "Send SMS notifications to parents or staff.",          "NORMAL",    False),    ("communication.email.send",        "communication","send",     "Send broadcast emails to school stakeholders.",                            "NORMAL",    False),    ("communication.announcement.post", "communication","post",     "Post announcements on the school notice board.",           "NORMAL",    False),    ("communication.chat.view",         "communication","view",     "View internal school messaging threads.",                  "NORMAL",    False),    ("communication.chat.send",         "communication","send",     "Send messages in internal school chat.",                   "NORMAL",    False),
    # ── ADMISSIONS ─────────────────────────────────────────────────────────
    ("admissions.application.view",     "admissions",   "view",     "View incoming admission applications.",             "NORMAL",    False),    ("admissions.application.process",  "admissions",   "process",  "Shortlist applicants and schedule interviews.",    "SENSITIVE", False),    ("admissions.application.approve",  "admissions",   "approve",  "Formally approve or reject admission applications.",          "SENSITIVE", True),    ("admissions.enrollment.confirm",   "admissions",   "confirm",  "Convert an accepted applicant to an enrolled student.",   "SENSITIVE", False),
    # ── HOSTEL / BOARDING ──────────────────────────────────────────────────
    ("hostel.room.view",                "hostel",       "view",     "View hostel room listings and student allocations.",        "NORMAL",    False),    ("hostel.room.manage",              "hostel",       "manage",   "Create rooms and allocate students to hostel rooms.",          "NORMAL",    False),    ("hostel.attendance.mark",          "hostel",       "mark",     "Conduct hostel roll call and record attendance.",                            "NORMAL",    False),    ("hostel.incident.report",          "hostel",       "report",   "Log a hostel or boarding incident.",                             "SENSITIVE", False),    ("hostel.incident.manage",          "hostel",       "manage",   "Review, resolve and escalate hostel incidents.",            "SENSITIVE", False),
    # ── TRANSPORT ──────────────────────────────────────────────────────────
    ("transport.routes.view",           "transport",    "view",     "View school bus routes and vehicle assignments.",               "NORMAL",    False),    ("transport.routes.manage",         "transport",    "manage",   "Create and edit transport routes and vehicle records.",           "NORMAL",    False),    ("transport.students.assign",       "transport",    "assign",   "Assign students to transport routes.",              "NORMAL",    False),    ("transport.tracking.view",         "transport",    "view",     "View live GPS or vehicle tracking data.",                   "NORMAL",    False),
    # ── CANTEEN / CAFETERIA ────────────────────────────────────────────────
    ("canteen.menu.view",               "canteen",      "view",     "View the canteen menu and pricing.",                     "NORMAL",    False),    ("canteen.menu.manage",             "canteen",      "manage",   "Update canteen menu items and prices.",                  "NORMAL",    False),    ("canteen.orders.manage",           "canteen",      "manage",   "Process and fulfil canteen orders.",                "NORMAL",    False),    ("canteen.sales.report",            "canteen",      "report",   "Generate canteen daily and weekly sales reports.",      "NORMAL",    False),
    # ── EVENTS ─────────────────────────────────────────────────────────────
    ("events.calendar.view",            "events",       "view",     "View the school events calendar.",                       "NORMAL",    False),    ("events.calendar.manage",          "events",       "manage",   "Create and edit events on the school calendar.",        "NORMAL",    False),    ("events.attendance.track",         "events",       "track",    "Record attendance at school events.",                "NORMAL",    False),
    # ── ALUMNI ─────────────────────────────────────────────────────────────
    ("alumni.profile.view",             "alumni",       "view",     "View the alumni directory and profiles.",                            "NORMAL",    False),    ("alumni.profile.manage",           "alumni",       "manage",   "Update alumni profile records.",                            "NORMAL",    False),    ("alumni.communications.send",      "alumni",       "send",     "Send communications and newsletters to alumni.",                    "NORMAL",    False),
    # ── SETTINGS / CONFIGURATION ───────────────────────────────────────────
    ("settings.school.view",            "settings",     "view",     "View school-wide configuration settings.",               "NORMAL",    False),    ("settings.school.manage",          "settings",     "manage",   "Edit school-wide configuration and branding.",                   "CRITICAL",  True),    ("settings.branch.view",            "settings",     "view",     "View branch-level configuration settings.",                        "NORMAL",    False),    ("settings.branch.manage",          "settings",     "manage",   "Edit branch-level configuration.",                  "SENSITIVE", True),    ("settings.academic_session.manage","settings",     "manage",   "Open, close and roll over academic sessions and terms.",           "CRITICAL",  True),    ("settings.roles.view",             "settings",     "view",     "View role and permission configurations for the school.",          "SENSITIVE", False),    ("settings.roles.manage",           "settings",     "manage",   "Create and edit school roles and assign permissions.",  "CRITICAL",  True),
    # ── REPORTS (cross-module) ─────────────────────────────────────────────
    ("reports.school_wide.view",        "reports",      "view",     "Access consolidated school-wide reports.",          "SENSITIVE", False),    ("reports.school_wide.export",      "reports",      "export",   "Export consolidated school-wide reports.",          "SENSITIVE", True),
    # ── AUDIT LOG ──────────────────────────────────────────────────────────
    ("audit.logs.view",                 "audit",        "view",     "View the school-scoped system audit trail.",                          "CRITICAL",  True),    ("audit.logs.export",               "audit",        "export",   "Export school-scoped audit log records.",                                "CRITICAL",  True),
    # ══════════════════════════════════════════════════════════════════════
    # PLATFORM-ONLY PERMISSIONS (Vision internal — used by PlatformRoleTemplate)
    # ══════════════════════════════════════════════════════════════════════

    # ── PLATFORM: SCHOOL MANAGEMENT ────────────────────────────────────────
    ("platform.schools.view",           "platform",     "view",     "View all schools registered on the XVS platform.",                 "SENSITIVE", False),    ("platform.schools.create",         "platform",     "create",   "Onboard a new school onto the XVS platform.",           "CRITICAL",  True),    ("platform.schools.update",         "platform",     "update",   "Update any school's profile and configuration.",          "CRITICAL",  True),    ("platform.schools.suspend",        "platform",     "suspend",  "Suspend or reactivate a school account platform-wide.",           "CRITICAL",  True),    ("platform.schools.delete",         "platform",     "delete",   "Permanently delete a school and all its data.",               "CRITICAL",  True),
    # ── PLATFORM: BILLING / SUBSCRIPTIONS ─────────────────────────────────
    ("platform.billing.view",           "platform",     "view",     "View school billing records and subscription plans.",     "CRITICAL",  True),    ("platform.billing.manage",         "platform",     "manage",   "Edit plans, issue credits and manage platform invoices.",       "CRITICAL",  True),    ("platform.billing.export",         "platform",     "export",   "Export platform billing and subscription data.",                              "CRITICAL",  True),
    # ── PLATFORM: USER MANAGEMENT ─────────────────────────────────────────
    ("platform.users.view",             "platform",     "view",     "View all platform and school-level user accounts.",       "SENSITIVE", False),    ("platform.users.impersonate",      "platform",     "impersonate","Impersonate a school user for audited support diagnostics.",          "CRITICAL",  True),    ("platform.users.suspend",          "platform",     "suspend",  "Suspend or reactivate any user account platform-wide.",              "CRITICAL",  True),    ("platform.users.delete",           "platform",     "delete",   "Permanently delete a user account.",                 "CRITICAL",  True),
    # ── PLATFORM: ROLES & PERMISSIONS ─────────────────────────────────────
    ("platform.roles.view",             "platform",     "view",     "View platform role templates and their permission sets.",     "SENSITIVE", False),    ("platform.roles.manage",           "platform",     "manage",   "Create and edit platform-level role templates.",              "CRITICAL",  True),    ("platform.permissions.manage",     "platform",     "manage",   "Add or remove entries in the global permission registry.",    "CRITICAL",  True),
    # ── PLATFORM: SUPPORT ─────────────────────────────────────────────────
    ("platform.support.tickets.view",   "platform",     "view",     "View support tickets submitted by all schools.",            "SENSITIVE", False),    ("platform.support.tickets.manage", "platform",     "manage",   "Respond to and resolve school support tickets.",           "SENSITIVE", False),    ("platform.support.escalate",       "platform",     "escalate", "Escalate a support ticket to engineering or leadership.",   "SENSITIVE", False),
    # ── PLATFORM: ANALYTICS & REPORTING ───────────────────────────────────
    ("platform.analytics.view",         "platform",     "view",     "View platform-wide analytics dashboards.",          "SENSITIVE", False),    ("platform.analytics.export",       "platform",     "export",   "Export platform-wide analytics datasets.",               "CRITICAL",  True),    ("platform.reports.financial",      "platform",     "view",     "View consolidated platform financial reports.",             "CRITICAL",  True),
    # ── PLATFORM: SYSTEM / INFRA ───────────────────────────────────────────
    ("platform.system.config.view",     "platform",     "view",     "View platform-level system configuration values.",               "CRITICAL",  True),    ("platform.system.config.manage",   "platform",     "manage",   "Edit platform-level system settings and config.",              "CRITICAL",  True),    ("platform.system.deployments.view","platform",     "view",     "View deployment history, release notes and rollout status.",        "SENSITIVE", False),    ("platform.system.deployments.trigger","platform",  "trigger",  "Trigger production deployments or initiate rollbacks.",                 "CRITICAL",  True),    ("platform.system.logs.view",       "platform",     "view",     "View application and server-side system logs.",                     "CRITICAL",  True),    ("platform.system.maintenance.manage","platform",   "manage",   "Enable or disable global maintenance mode.",         "CRITICAL",  True),    ("platform.integrations.view",      "platform",     "view",     "View third-party integration configurations.",      "SENSITIVE", False),    ("platform.integrations.manage",    "platform",     "manage",   "Add, edit and revoke platform-level integrations.",            "CRITICAL",  True),
    # ── PLATFORM: COMPLIANCE & AUDIT ──────────────────────────────────────
    ("platform.compliance.view",        "platform",     "view",     "View compliance frameworks, checklists and evidence.",        "CRITICAL",  True),    ("platform.compliance.manage",      "platform",     "manage",   "Create and update compliance records and evidence packs.",           "CRITICAL",  True),    ("platform.audit.logs.view",        "platform",     "view",     "View the platform-wide immutable audit log.",                     "CRITICAL",  True),    ("platform.audit.logs.export",      "platform",     "export",   "Export platform-wide audit log records.",                       "CRITICAL",  True),
    # ── PLATFORM: DATA ENGINEERING ────────────────────────────────────────
    ("platform.data.pipelines.view",    "platform",     "view",     "View data pipeline definitions and run statuses.",                      "SENSITIVE", False),    ("platform.data.pipelines.manage",  "platform",     "manage",   "Create, edit and trigger data pipeline runs.",               "CRITICAL",  True),    ("platform.data.migrations.run",    "platform",     "run",      "Execute database schema and data migrations.",               "CRITICAL",  True),    ("platform.data.backups.view",      "platform",     "view",     "View backup schedules and available restore points.",         "CRITICAL",  True),    ("platform.data.backups.manage",    "platform",     "manage",   "Create, schedule and restore from database backups.",            "CRITICAL",  True),
    # ── PLATFORM: SECURITY ────────────────────────────────────────────────
    ("platform.security.incidents.view","platform",     "view",     "View security incident records and investigation notes.",                   "CRITICAL",  True),    ("platform.security.incidents.manage","platform",   "manage",   "Investigate, update and resolve security incidents.",       "CRITICAL",  True),    ("platform.security.pen_test.manage","platform",    "manage",   "Manage penetration test plans, scopes and findings.",       "CRITICAL",  True),]


# ---------------------------------------------------------------------------
# 2. PERMISSION DEPENDENCIES
#    Format: (permission_key, depends_on_key)
#
#    Each tuple says "you cannot hold <permission_key> unless you also hold
#    <depends_on_key>". Dependency checks run against the *flattened* role
#    permission set so a role can satisfy a dependency via a direct grant or
#    via a permission group attached to the role.
# ---------------------------------------------------------------------------

PERMISSION_DEPENDENCIES: list[tuple[str, str]] = [

    # ── STUDENTS ───────────────────────────────────────────────────────────
    ("students.profile.update",     "students.profile.view"),
    ("students.profile.delete",     "students.profile.view"),
    ("students.profile.export",     "students.profile.view"),
    ("students.class.assign",       "students.profile.view"),
    ("students.class.transfer",     "students.profile.view"),
    ("students.disciplinary.manage","students.disciplinary.view"),
    ("students.medical.manage",     "students.medical.view"),
    ("students.guardian.manage",    "students.guardian.view"),
    ("students.id_card.generate",   "students.profile.view"),

    # ── STAFF ──────────────────────────────────────────────────────────────
    ("staff.profile.update",        "staff.profile.view"),
    ("staff.profile.delete",        "staff.profile.view"),
    ("staff.profile.export",        "staff.profile.view"),
    ("staff.salary.manage",         "staff.salary.view"),
    ("staff.appraisal.manage",      "staff.appraisal.view"),
    ("staff.attendance.mark",       "staff.attendance.view"),

    # ── ACADEMICS ──────────────────────────────────────────────────────────
    ("academics.curriculum.manage", "academics.curriculum.view"),
    ("academics.timetable.manage",  "academics.timetable.view"),
    ("academics.subjects.manage",   "academics.subjects.view"),
    ("academics.class.manage",      "academics.class.view"),
    ("academics.lesson_notes.manage","academics.lesson_notes.view"),
    ("academics.lesson_notes.approve","academics.lesson_notes.view"),

    # ── ASSESSMENTS ────────────────────────────────────────────────────────
    ("assessments.scores.edit",     "assessments.scores.view"),
    ("assessments.scores.approve",  "assessments.scores.view"),
    ("assessments.results.publish", "assessments.scores.view"),
    ("assessments.results.export",  "assessments.scores.view"),
    ("assessments.report_card.print","assessments.scores.view"),
    ("assessments.exam_schedule.manage","assessments.exam_schedule.view"),

    # ── ATTENDANCE ─────────────────────────────────────────────────────────
    ("attendance.student.mark",     "attendance.student.view"),
    ("attendance.student.edit",     "attendance.student.view"),
    ("attendance.student.export",   "attendance.student.view"),
    ("attendance.student.report",   "attendance.student.view"),

    # ── FINANCE ────────────────────────────────────────────────────────────
    ("finance.fees.manage",         "finance.fees.view"),
    ("finance.fees.waive",          "finance.fees.view"),
    ("finance.invoice.create",      "finance.invoice.view"),
    ("finance.invoice.approve",     "finance.invoice.view"),
    ("finance.payment.record",      "finance.fees.view"),
    ("finance.payment.verify",      "finance.invoice.view"),
    ("finance.payment.reverse",     "finance.invoice.view"),
    ("finance.expenditure.manage",  "finance.expenditure.view"),
    ("finance.reports.export",      "finance.reports.view"),
    ("finance.budget.manage",       "finance.budget.view"),

    # ── LIBRARY ────────────────────────────────────────────────────────────
    ("library.catalog.manage",      "library.catalog.view"),
    ("library.borrow.record",       "library.catalog.view"),
    ("library.borrow.return",       "library.catalog.view"),
    ("library.overdue.manage",      "library.catalog.view"),

    # ── HEALTH ─────────────────────────────────────────────────────────────
    ("health.visits.record",        "health.visits.view"),
    ("health.medication.manage",    "health.visits.view"),

    # ── ADMISSIONS ─────────────────────────────────────────────────────────
    ("admissions.application.process","admissions.application.view"),
    ("admissions.application.approve","admissions.application.view"),
    ("admissions.enrollment.confirm", "admissions.application.view"),

    # ── HOSTEL ─────────────────────────────────────────────────────────────
    ("hostel.room.manage",          "hostel.room.view"),
    ("hostel.incident.manage",      "hostel.incident.report"),

    # ── TRANSPORT ──────────────────────────────────────────────────────────
    ("transport.routes.manage",     "transport.routes.view"),
    ("transport.students.assign",   "transport.routes.view"),

    # ── CANTEEN ────────────────────────────────────────────────────────────
    ("canteen.menu.manage",         "canteen.menu.view"),

    # ── EVENTS ─────────────────────────────────────────────────────────────
    ("events.calendar.manage",      "events.calendar.view"),
    ("events.attendance.track",     "events.calendar.view"),

    # ── ALUMNI ─────────────────────────────────────────────────────────────
    ("alumni.profile.manage",       "alumni.profile.view"),
    ("alumni.communications.send",  "alumni.profile.view"),

    # ── SETTINGS ───────────────────────────────────────────────────────────
    ("settings.school.manage",      "settings.school.view"),
    ("settings.branch.manage",      "settings.branch.view"),
    ("settings.roles.manage",       "settings.roles.view"),

    # ── REPORTS ────────────────────────────────────────────────────────────
    ("reports.school_wide.export",  "reports.school_wide.view"),

    # ── AUDIT ──────────────────────────────────────────────────────────────
    ("audit.logs.export",           "audit.logs.view"),

    # ── PLATFORM ───────────────────────────────────────────────────────────
    ("platform.schools.create",     "platform.schools.view"),
    ("platform.schools.update",     "platform.schools.view"),
    ("platform.schools.suspend",    "platform.schools.view"),
    ("platform.schools.delete",     "platform.schools.view"),
    ("platform.billing.manage",     "platform.billing.view"),
    ("platform.billing.export",     "platform.billing.view"),
    ("platform.users.impersonate",  "platform.users.view"),
    ("platform.users.suspend",      "platform.users.view"),
    ("platform.users.delete",       "platform.users.view"),
    ("platform.roles.manage",       "platform.roles.view"),
    ("platform.support.tickets.manage","platform.support.tickets.view"),
    ("platform.support.escalate",   "platform.support.tickets.view"),
    ("platform.analytics.export",   "platform.analytics.view"),
    ("platform.system.config.manage","platform.system.config.view"),
    ("platform.system.deployments.trigger","platform.system.deployments.view"),
    ("platform.integrations.manage","platform.integrations.view"),
    ("platform.compliance.manage",  "platform.compliance.view"),
    ("platform.audit.logs.export",  "platform.audit.logs.view"),
    ("platform.data.pipelines.manage","platform.data.pipelines.view"),
    ("platform.data.backups.manage","platform.data.backups.view"),
    ("platform.security.incidents.manage","platform.security.incidents.view"),
]


# ---------------------------------------------------------------------------
# 3. PERMISSION GROUPS
#    Reusable permission bundles shared across school and platform role
#    templates. Each group is a named, discoverable bucket of permissions
#    that one or more roles attach to.
#
#    Format: dict(name, description, permissions=[key, ...])
# ---------------------------------------------------------------------------

PERMISSION_GROUPS: list[dict] = [

    # ── CORE / SHARED ──────────────────────────────────────────────────────
    {
        "name": "Dashboard - Basic",
        "description": "Landing dashboard access for any authenticated user.",
        "permissions": [
            "dashboard.overview.view",
        ],
    },
    {
        "name": "Dashboard - Analytics",
        "description": "Dashboard plus analytics widgets for leadership.",
        "permissions": [
            "dashboard.overview.view",
            "dashboard.analytics.view",
        ],
    },
    {
        "name": "Announcements - Post",
        "description": "Post announcements on the notice board.",
        "permissions": [
            "communication.announcement.post",
        ],
    },
    {
        "name": "Announcements - Manage",
        "description": "Manage dashboard announcements and notice board.",
        "permissions": [
            "dashboard.announcements.manage",
            "communication.announcement.post",
        ],
    },
    {
        "name": "Internal Chat",
        "description": "Use the built-in internal messaging system.",
        "permissions": [
            "communication.chat.view",
            "communication.chat.send",
        ],
    },
    {
        "name": "Messaging - Broadcast",
        "description": "Send SMS and email broadcasts to parents/staff.",
        "permissions": [
            "communication.sms.send",
            "communication.email.send",
        ],
    },

    # ── STUDENTS ───────────────────────────────────────────────────────────
    {
        "name": "Students - View",
        "description": "Read-only access to student profiles and guardian records.",
        "permissions": [
            "students.profile.view",
            "students.guardian.view",
        ],
    },
    {
        "name": "Students - Manage",
        "description": "Full create/update authority over student and guardian records.",
        "permissions": [
            "students.profile.view",
            "students.profile.create",
            "students.profile.update",
            "students.profile.export",
            "students.class.assign",
            "students.class.transfer",
            "students.guardian.view",
            "students.guardian.manage",
            "students.id_card.generate",
        ],
    },
    {
        "name": "Students - Enrolment Admin",
        "description": "Admissions-oriented student admin (no class transfer, no export).",
        "permissions": [
            "students.profile.view",
            "students.profile.create",
            "students.profile.update",
            "students.guardian.view",
            "students.guardian.manage",
            "students.id_card.generate",
        ],
    },
    {
        "name": "Students - Discipline",
        "description": "Manage student disciplinary records.",
        "permissions": [
            "students.profile.view",
            "students.disciplinary.view",
            "students.disciplinary.manage",
        ],
    },
    {
        "name": "Students - Discipline (Read)",
        "description": "Read-only access to disciplinary records.",
        "permissions": [
            "students.profile.view",
            "students.disciplinary.view",
        ],
    },
    {
        "name": "Students - Medical (Read)",
        "description": "Read-only access to student medical records.",
        "permissions": [
            "students.profile.view",
            "students.medical.view",
        ],
    },
    {
        "name": "Students - Medical (Manage)",
        "description": "Full access to student medical records (nurse/clinician).",
        "permissions": [
            "students.profile.view",
            "students.medical.view",
            "students.medical.manage",
        ],
    },

    # ── STAFF ──────────────────────────────────────────────────────────────
    {
        "name": "Staff - View",
        "description": "Read-only access to staff directory and attendance.",
        "permissions": [
            "staff.profile.view",
            "staff.attendance.view",
        ],
    },
    {
        "name": "Staff - Manage",
        "description": "Full authority to onboard and edit staff records.",
        "permissions": [
            "staff.profile.view",
            "staff.profile.create",
            "staff.profile.update",
            "staff.profile.export",
            "staff.attendance.view",
            "staff.attendance.mark",
        ],
    },
    {
        "name": "Staff - Leave Approve",
        "description": "Approve or reject staff leave requests.",
        "permissions": [
            "staff.leave.approve",
        ],
    },
    {
        "name": "Staff - Appraisal",
        "description": "Conduct and submit staff appraisals.",
        "permissions": [
            "staff.profile.view",
            "staff.appraisal.view",
            "staff.appraisal.manage",
        ],
    },
    {
        "name": "Staff - Appraisal (Read)",
        "description": "Read-only access to staff appraisal records.",
        "permissions": [
            "staff.profile.view",
            "staff.appraisal.view",
        ],
    },
    {
        "name": "Staff - Payroll (Read)",
        "description": "Read-only access to payroll entries (leadership).",
        "permissions": [
            "staff.profile.view",
            "staff.salary.view",
        ],
    },
    {
        "name": "Staff - Payroll (Manage)",
        "description": "Full authority over payroll entries.",
        "permissions": [
            "staff.profile.view",
            "staff.salary.view",
            "staff.salary.manage",
        ],
    },

    # ── ACADEMICS ──────────────────────────────────────────────────────────
    {
        "name": "Academics - View",
        "description": "Read curriculum, timetable, subjects, classes and lesson notes.",
        "permissions": [
            "academics.curriculum.view",
            "academics.timetable.view",
            "academics.subjects.view",
            "academics.class.view",
            "academics.lesson_notes.view",
        ],
    },
    {
        "name": "Academics - Structure Manage",
        "description": "Manage curriculum, timetable, subjects, classes (no lesson notes).",
        "permissions": [
            "academics.curriculum.view",
            "academics.curriculum.manage",
            "academics.timetable.view",
            "academics.timetable.manage",
            "academics.subjects.view",
            "academics.subjects.manage",
            "academics.class.view",
            "academics.class.manage",
        ],
    },
    {
        "name": "Academics - Lesson Notes (Manage)",
        "description": "Upload and edit lesson notes.",
        "permissions": [
            "academics.lesson_notes.view",
            "academics.lesson_notes.manage",
        ],
    },
    {
        "name": "Academics - Lesson Notes (Approve)",
        "description": "Approve lesson notes before publishing.",
        "permissions": [
            "academics.lesson_notes.view",
            "academics.lesson_notes.approve",
        ],
    },
    {
        "name": "Academics - Portal View",
        "description": "Student/parent portal view of academics (timetable, subjects, lesson notes).",
        "permissions": [
            "academics.timetable.view",
            "academics.subjects.view",
            "academics.lesson_notes.view",
        ],
    },

    # ── ASSESSMENTS ────────────────────────────────────────────────────────
    {
        "name": "Assessments - Teacher",
        "description": "Enter and view scores for assigned subjects.",
        "permissions": [
            "assessments.scores.view",
            "assessments.scores.enter",
            "assessments.exam_schedule.view",
        ],
    },
    {
        "name": "Assessments - Approve",
        "description": "Approve/ratify score sheets submitted by teachers.",
        "permissions": [
            "assessments.scores.view",
            "assessments.scores.approve",
        ],
    },
    {
        "name": "Assessments - Exam Office",
        "description": "Full exam processing, results publication, and grading configuration.",
        "permissions": [
            "assessments.scores.view",
            "assessments.scores.enter",
            "assessments.scores.edit",
            "assessments.scores.approve",
            "assessments.results.publish",
            "assessments.results.export",
            "assessments.report_card.print",
            "assessments.exam_schedule.view",
            "assessments.exam_schedule.manage",
            "assessments.grading.manage",
        ],
    },
    {
        "name": "Assessments - Leadership",
        "description": "Leadership oversight of scores, results, and report cards.",
        "permissions": [
            "assessments.scores.view",
            "assessments.scores.approve",
            "assessments.results.publish",
            "assessments.results.export",
            "assessments.report_card.print",
            "assessments.exam_schedule.view",
            "assessments.exam_schedule.manage",
        ],
    },
    {
        "name": "Assessments - Portal View",
        "description": "Read-only results and schedule for students/parents.",
        "permissions": [
            "assessments.scores.view",
            "assessments.report_card.print",
            "assessments.exam_schedule.view",
        ],
    },

    # ── ATTENDANCE ─────────────────────────────────────────────────────────
    {
        "name": "Attendance - Teacher",
        "description": "Mark daily/period student attendance.",
        "permissions": [
            "attendance.student.view",
            "attendance.student.mark",
            "attendance.student.report",
        ],
    },
    {
        "name": "Attendance - Manage",
        "description": "Mark, edit, export and report on attendance.",
        "permissions": [
            "attendance.student.view",
            "attendance.student.mark",
            "attendance.student.edit",
            "attendance.student.export",
            "attendance.student.report",
        ],
    },
    {
        "name": "Attendance - Report",
        "description": "Read-only attendance with reporting + export.",
        "permissions": [
            "attendance.student.view",
            "attendance.student.report",
            "attendance.student.export",
        ],
    },
    {
        "name": "Attendance - View Only",
        "description": "Read-only attendance access (no edits).",
        "permissions": [
            "attendance.student.view",
            "attendance.student.report",
        ],
    },
    {
        "name": "Attendance - Portal View",
        "description": "Basic attendance read for students/parents.",
        "permissions": [
            "attendance.student.view",
        ],
    },

    # ── FINANCE ────────────────────────────────────────────────────────────
    {
        "name": "Finance - Clerk",
        "description": "Day-to-day invoice creation and payment entry.",
        "permissions": [
            "finance.fees.view",
            "finance.invoice.view",
            "finance.invoice.create",
            "finance.payment.record",
            "finance.payment.verify",
        ],
    },
    {
        "name": "Finance - Admin",  # UPDATED: replaced job title with role title
        "description": "Full finance administration including approval and waivers.",
        "permissions": [
            "finance.fees.view",
            "finance.fees.manage",
            "finance.fees.waive",
            "finance.invoice.view",
            "finance.invoice.create",
            "finance.invoice.approve",
            "finance.payment.record",
            "finance.payment.verify",
            "finance.payment.reverse",
            "finance.expenditure.view",
            "finance.expenditure.manage",
            "finance.reports.view",
            "finance.reports.export",
            "finance.budget.view",
            "finance.budget.manage",
        ],
    },
    {
        "name": "Finance - Leadership Read",
        "description": "Read finance summaries plus payment entry for leadership.",
        "permissions": [
            "finance.fees.view",
            "finance.invoice.view",
            "finance.payment.record",
            "finance.payment.verify",
            "finance.expenditure.view",
            "finance.reports.view",
            "finance.reports.export",
            "finance.budget.view",
        ],
    },
    {
        "name": "Finance - Portal View",
        "description": "Fees and invoices read-only for students/parents.",
        "permissions": [
            "finance.fees.view",
            "finance.invoice.view",
        ],
    },

    # ── LIBRARY ────────────────────────────────────────────────────────────
    {
        "name": "Library - Staff",
        "description": "Full library catalogue and loan management.",
        "permissions": [
            "library.catalog.view",
            "library.catalog.manage",
            "library.borrow.record",
            "library.borrow.return",
            "library.overdue.manage",
            "library.reports.view",
        ],
    },
    {
        "name": "Library - Leadership Read",
        "description": "Read the library catalogue and usage reports.",
        "permissions": [
            "library.catalog.view",
            "library.reports.view",
        ],
    },
    {
        "name": "Library - Member",
        "description": "Browse the library catalogue.",
        "permissions": [
            "library.catalog.view",
        ],
    },

    # ── HEALTH ─────────────────────────────────────────────────────────────
    {
        "name": "Health - Nurse",
        "description": "Full sick-bay, medication and reporting access.",
        "permissions": [
            "health.visits.view",
            "health.visits.record",
            "health.medication.manage",
            "health.reports.view",
        ],
    },
    {
        "name": "Health - Leadership Read",
        "description": "Read-only sick-bay logs and health reports.",
        "permissions": [
            "health.visits.view",
            "health.reports.view",
        ],
    },
    {
        "name": "Health - Visits (Read)",
        "description": "Read-only access to sick-bay visit logs.",
        "permissions": [
            "health.visits.view",
        ],
    },

    # ── ADMISSIONS ─────────────────────────────────────────────────────────
    {
        "name": "Admissions - Processing",
        "description": "Process applications through interview and enrolment.",
        "permissions": [
            "admissions.application.view",
            "admissions.application.process",
            "admissions.enrollment.confirm",
        ],
    },
    {
        "name": "Admissions - Approve",
        "description": "Approve/reject applications and confirm enrolment.",
        "permissions": [
            "admissions.application.view",
            "admissions.application.process",
            "admissions.application.approve",
            "admissions.enrollment.confirm",
        ],
    },

    # ── HOSTEL ─────────────────────────────────────────────────────────────
    {
        "name": "Hostel - Manage",
        "description": "Manage rooms, attendance and incidents in boarding houses.",
        "permissions": [
            "hostel.room.view",
            "hostel.room.manage",
            "hostel.attendance.mark",
            "hostel.incident.report",
            "hostel.incident.manage",
        ],
    },
    {
        "name": "Hostel - Leadership Read",
        "description": "Leadership oversight of hostel operations.",
        "permissions": [
            "hostel.room.view",
            "hostel.incident.report",
            "hostel.incident.manage",
        ],
    },
    {
        "name": "Hostel - Rooms View",
        "description": "Read-only access to hostel room listings.",
        "permissions": [
            "hostel.room.view",
        ],
    },

    # ── TRANSPORT ──────────────────────────────────────────────────────────
    {
        "name": "Transport - Coordinator",
        "description": "Manage transport routes, assignments and tracking.",
        "permissions": [
            "transport.routes.view",
            "transport.routes.manage",
            "transport.students.assign",
            "transport.tracking.view",
        ],
    },
    {
        "name": "Transport - Leadership",
        "description": "Leadership oversight of transport routes and tracking.",
        "permissions": [
            "transport.routes.view",
            "transport.students.assign",
            "transport.tracking.view",
        ],
    },
    {
        "name": "Transport - View",
        "description": "Read-only transport routes and live tracking.",
        "permissions": [
            "transport.routes.view",
            "transport.tracking.view",
        ],
    },
    {
        "name": "Transport - Routes Only",
        "description": "Read-only transport routes (no tracking).",
        "permissions": [
            "transport.routes.view",
        ],
    },

    # ── CANTEEN ────────────────────────────────────────────────────────────
    {
        "name": "Canteen - Manage",
        "description": "Manage canteen menu, orders and sales.",
        "permissions": [
            "canteen.menu.view",
            "canteen.menu.manage",
            "canteen.orders.manage",
            "canteen.sales.report",
        ],
    },

    # ── EVENTS ─────────────────────────────────────────────────────────────
    {
        "name": "Events - Manage",
        "description": "Manage events calendar and event attendance tracking.",
        "permissions": [
            "events.calendar.view",
            "events.calendar.manage",
            "events.attendance.track",
        ],
    },
    {
        "name": "Events - Calendar Manage",
        "description": "Create and edit events on the school calendar.",
        "permissions": [
            "events.calendar.view",
            "events.calendar.manage",
        ],
    },
    {
        "name": "Events - View",
        "description": "Read-only events calendar.",
        "permissions": [
            "events.calendar.view",
        ],
    },

    # ── ALUMNI ─────────────────────────────────────────────────────────────
    {
        "name": "Alumni - Manage",
        "description": "Manage alumni directory and outbound communications.",
        "permissions": [
            "alumni.profile.view",
            "alumni.profile.manage",
            "alumni.communications.send",
        ],
    },
    {
        "name": "Alumni - View",
        "description": "Read-only alumni directory.",
        "permissions": [
            "alumni.profile.view",
        ],
    },

    # ── SETTINGS / ROLES ───────────────────────────────────────────────────
    {
        "name": "Settings - View",
        "description": "Read-only school and branch configuration.",
        "permissions": [
            "settings.school.view",
            "settings.branch.view",
        ],
    },
    {
        "name": "Settings - School Admin",
        "description": "Manage school/branch settings and academic sessions.",
        "permissions": [
            "settings.school.view",
            "settings.school.manage",
            "settings.branch.view",
            "settings.branch.manage",
            "settings.academic_session.manage",
        ],
    },
    {
        "name": "Settings - Academic Session",
        "description": "Open/close academic sessions and terms.",
        "permissions": [
            "settings.academic_session.manage",
        ],
    },
    {
        "name": "Settings - Roles (Read)",
        "description": "Read role and permission configuration.",
        "permissions": [
            "settings.roles.view",
        ],
    },
    {
        "name": "Settings - Roles (Manage)",
        "description": "Create/edit school roles and assign permissions.",
        "permissions": [
            "settings.roles.view",
            "settings.roles.manage",
        ],
    },

    # ── REPORTS / AUDIT ────────────────────────────────────────────────────
    {
        "name": "Reports - School-wide Read",
        "description": "Read consolidated school-wide reports.",
        "permissions": [
            "reports.school_wide.view",
        ],
    },
    {
        "name": "Reports - School-wide Export",
        "description": "Read and export consolidated school-wide reports.",
        "permissions": [
            "reports.school_wide.view",
            "reports.school_wide.export",
        ],
    },
    {
        "name": "Audit - Read",
        "description": "View the system audit trail.",
        "permissions": [
            "audit.logs.view",
        ],
    },
    {
        "name": "Audit - Export",
        "description": "View and export audit logs.",
        "permissions": [
            "audit.logs.view",
            "audit.logs.export",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # PLATFORM-SIDE GROUPS (used by PlatformRoleTemplate)
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "Platform - Schools (Read)",
        "description": "Read all schools on the platform.",
        "permissions": [
            "platform.schools.view",
        ],
    },
    {
        "name": "Platform - Schools (Manage)",
        "description": "Create, update, suspend and delete schools.",
        "permissions": [
            "platform.schools.view",
            "platform.schools.create",
            "platform.schools.update",
            "platform.schools.suspend",
            "platform.schools.delete",
        ],
    },
    {
        "name": "Platform - Schools (Onboard)",
        "description": "Create and update schools (no suspend/delete).",
        "permissions": [
            "platform.schools.view",
            "platform.schools.create",
            "platform.schools.update",
        ],
    },
    {
        "name": "Platform - Users (Read)",
        "description": "Read platform and school user accounts.",
        "permissions": [
            "platform.users.view",
        ],
    },
    {
        "name": "Platform - Users (Elevated)",
        "description": "Impersonate, suspend and delete user accounts.",
        "permissions": [
            "platform.users.view",
            "platform.users.impersonate",
            "platform.users.suspend",
            "platform.users.delete",
        ],
    },
    {
        "name": "Platform - Users (Support)",
        "description": "View + impersonate users for advanced support.",
        "permissions": [
            "platform.users.view",
            "platform.users.impersonate",
        ],
    },
    {
        "name": "Platform - Roles (Read)",
        "description": "Read platform role templates.",
        "permissions": [
            "platform.roles.view",
        ],
    },
    {
        "name": "Platform - Roles (Manage)",
        "description": "Manage platform role templates and permission registry.",
        "permissions": [
            "platform.roles.view",
            "platform.roles.manage",
            "platform.permissions.manage",
        ],
    },
    {
        "name": "Platform - Billing (Read)",
        "description": "Read-only billing access.",
        "permissions": [
            "platform.billing.view",
        ],
    },
    {
        "name": "Platform - Billing (Full)",
        "description": "Full billing administration plus financial reporting.",
        "permissions": [
            "platform.billing.view",
            "platform.billing.manage",
            "platform.billing.export",
            "platform.reports.financial",
        ],
    },
    {
        "name": "Platform - Support (Tier 1)",
        "description": "Handle support tickets without impersonation.",
        "permissions": [
            "platform.support.tickets.view",
            "platform.support.tickets.manage",
        ],
    },
    {
        "name": "Platform - Support (Escalate)",
        "description": "Escalate tickets beyond tier 1.",
        "permissions": [
            "platform.support.tickets.view",
            "platform.support.tickets.manage",
            "platform.support.escalate",
        ],
    },
    {
        "name": "Platform - Support (Tickets Read)",
        "description": "Read-only access to support tickets.",
        "permissions": [
            "platform.support.tickets.view",
        ],
    },
    {
        "name": "Platform - Analytics (Read)",
        "description": "Read platform analytics dashboards.",
        "permissions": [
            "platform.analytics.view",
        ],
    },
    {
        "name": "Platform - Analytics (Full)",
        "description": "Read and export analytics plus financial reports.",
        "permissions": [
            "platform.analytics.view",
            "platform.analytics.export",
            "platform.reports.financial",
        ],
    },
    {
        "name": "Platform - Financial Reports",
        "description": "Read platform-wide financial reports.",
        "permissions": [
            "platform.reports.financial",
        ],
    },
    {
        "name": "Platform - System (Read)",
        "description": "Read system configuration, deployments, logs and integrations.",
        "permissions": [
            "platform.system.config.view",
            "platform.system.deployments.view",
            "platform.system.logs.view",
            "platform.integrations.view",
        ],
    },
    {
        "name": "Platform - System (Manage)",
        "description": "Manage system config, deployments, integrations and maintenance mode.",
        "permissions": [
            "platform.system.config.view",
            "platform.system.config.manage",
            "platform.system.deployments.view",
            "platform.system.deployments.trigger",
            "platform.system.logs.view",
            "platform.system.maintenance.manage",
            "platform.integrations.view",
            "platform.integrations.manage",
        ],
    },
    {
        "name": "Platform - System Logs",
        "description": "Read system and application logs.",
        "permissions": [
            "platform.system.logs.view",
        ],
    },
    {
        "name": "Platform - Data (Read)",
        "description": "Read pipelines and backup status.",
        "permissions": [
            "platform.data.pipelines.view",
            "platform.data.backups.view",
        ],
    },
    {
        "name": "Platform - Data (Manage)",
        "description": "Manage pipelines, run migrations and manage backups.",
        "permissions": [
            "platform.data.pipelines.view",
            "platform.data.pipelines.manage",
            "platform.data.migrations.run",
            "platform.data.backups.view",
            "platform.data.backups.manage",
        ],
    },
    {
        "name": "Platform - Security",
        "description": "Investigate incidents and manage pen-test findings.",
        "permissions": [
            "platform.security.incidents.view",
            "platform.security.incidents.manage",
            "platform.security.pen_test.manage",
        ],
    },
    {
        "name": "Platform - Compliance",
        "description": "Manage compliance records plus export audit logs.",
        "permissions": [
            "platform.compliance.view",
            "platform.compliance.manage",
            "platform.audit.logs.view",
            "platform.audit.logs.export",
        ],
    },
    {
        "name": "Platform - Audit (Read)",
        "description": "Read platform audit logs.",
        "permissions": [
            "platform.audit.logs.view",
        ],
    },
    {
        "name": "Platform - Audit (Export)",
        "description": "Read and export platform audit logs.",
        "permissions": [
            "platform.audit.logs.view",
            "platform.audit.logs.export",
        ],
    },
    {
        "name": "Platform - Integrations (Read)",
        "description": "Read third-party integration configuration.",
        "permissions": [
            "platform.integrations.view",
        ],
    },
]


# ---------------------------------------------------------------------------
# 4. SCHOOL ROLE TEMPLATES
#    Each role declares:
#      * name, description, is_system_role
#      * groups       : list of group names to attach
#      * permissions  : canonical full permission set for this role
#
#    The seeder attaches the groups, then creates direct ``SchoolRolePermission``
#    rows only for permissions in ``permissions`` that are not already
#    covered by the attached groups. The resulting effective set is
#    identical to ``permissions``.
# ---------------------------------------------------------------------------

# School roles are now created per-institution from PrebuiltSchoolRoleTemplate.
# This list is intentionally empty — the old seeder-created records are
# removed at step 4 below.
SCHOOL_ROLES: list[dict] = []


# ---------------------------------------------------------------------------
# 5. PLATFORM ROLE TEMPLATES (Vision-owned / tech-org context)
# ---------------------------------------------------------------------------

PLATFORM_ROLES: list[dict] = [

    {
        "name": "Vision Super Admin",
        "description": (
            "Unrestricted platform owner account. Full access across all modules, "
            "all schools, billing, system configuration, deployments, and audit logs. "
            "Held by a very small number of founding team members."
        ),
        "is_system_role": True,
        "is_locked": True,
        "groups": [
            "Platform - Schools (Manage)",
            "Platform - Users (Elevated)",
            "Platform - Roles (Manage)",
            "Platform - Billing (Full)",
            "Platform - Support (Escalate)",
            "Platform - Analytics (Full)",
            "Platform - System (Manage)",
            "Platform - Data (Manage)",
            "Platform - Security",
            "Platform - Compliance",
            "Platform - Audit (Export)",
        ],
        "permissions": [p[0] for p in PERMISSIONS if p[0].startswith("platform.")],
    },

    {
        "name": "Vision Platform Admin",
        "description": (
            "Full operational access to tenant management, provisioning, configuration, "
            "and module flags. Can suspend, restore, and reset tenants. Cannot perform "
            "hard deletes or override audit logs (Super Admin only)."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Manage)",
            "Platform - Users (Elevated)",
            "Platform - Roles (Manage)",
            "Platform - Analytics (Full)",
            "Platform - System (Manage)",
            "Platform - Data (Manage)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view", "platform.schools.create",
            "platform.schools.update", "platform.schools.suspend", "platform.schools.restore",
            "platform.users.view", "platform.users.manage",
            "platform.roles.view", "platform.roles.manage",
            "platform.analytics.view", "platform.analytics.export",
            "platform.system.config.view", "platform.system.config.manage",
            "platform.system.deployments.view", "platform.system.deployments.trigger",
            "platform.system.logs.view", "platform.system.maintenance.manage",
            "platform.integrations.view", "platform.integrations.manage",
            "platform.data.pipelines.view", "platform.data.pipelines.manage",
            "platform.data.migrations.run",
            "platform.data.backups.view", "platform.data.backups.manage",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Backend Engineer",
        "description": (
            "Software engineer building and maintaining XVS API and services. "
            "Has read access to platform and school configs for debugging. "
            "No access to billing or user management actions."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - Roles (Read)",
            "Platform - System (Read)",
            "Platform - Data (Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.roles.view",
            "platform.system.config.view",
            "platform.system.deployments.view",
            "platform.system.logs.view",
            "platform.integrations.view",
            "platform.data.pipelines.view",
            "platform.data.backups.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "DevOps Engineer",
        "description": (
            "Manages infrastructure dashboards, deployment pipelines, error tracking, "
            "and database health metrics. Can trigger deployments and manage server scaling. "
            "No access to institution data or billing."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - System (Manage)",
            "Platform - Data (Manage)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.system.config.view", "platform.system.config.manage",
            "platform.system.deployments.view", "platform.system.deployments.trigger",
            "platform.system.logs.view", "platform.system.maintenance.manage",
            "platform.data.pipelines.view", "platform.data.pipelines.manage",
            "platform.data.migrations.run",
            "platform.data.backups.view", "platform.data.backups.manage",
            "platform.integrations.view", "platform.integrations.manage",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "QA Engineer",
        "description": (
            "Full access to the staging environment with test tenant access. "
            "Can create, configure, and reset test institutions. "
            "Zero production access by default."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - Roles (Read)",
            # "Platform - System (Read)" omitted: QA doesn't need
            # platform.integrations.view. System config/deployments/logs perms
            # are applied as residual direct grants below.
            "Platform - Data (Read)",
            "Platform - Analytics (Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.roles.view",
            "platform.system.config.view",
            "platform.system.deployments.view",
            "platform.system.logs.view",
            "platform.data.pipelines.view",
            "platform.data.backups.view",
            "platform.audit.logs.view",
            "platform.analytics.view",
        ],
    },

    {
        "name": "Product Manager",
        "description": (
            "Drives roadmap and feature delivery. Accesses analytics, school-level "
            "data for insights, and platform reporting. No access to system config, "
            "deployments, or billing."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - Roles (Read)",
            "Platform - Analytics (Full)",
            "Platform - Support (Tickets Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.roles.view",
            "platform.analytics.view", "platform.analytics.export",
            "platform.reports.financial",
            "platform.support.tickets.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Data Operations Specialist",
        "description": (
            "Access to the import and validation engine. Can upload, validate, clean, "
            "and execute bulk data imports for institutions. Can view and remediate import "
            "job errors. No access to tenant configuration or RBAC settings."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Analytics (Full)",
            "Platform - Data (Read)",
            "Platform - Audit (Export)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.analytics.view", "platform.analytics.export",
            "platform.reports.financial",
            "platform.data.pipelines.view",
            "platform.data.backups.view",
            "platform.audit.logs.view", "platform.audit.logs.export",
        ],
    },

    {
        "name": "Support Agent",
        "description": (
            "First-line support agent responding to school tickets. Can view school "
            "profiles and user accounts for diagnostics. No impersonation or deletion rights."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - Support (Tier 1)",
            "Platform - Analytics (Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.support.tickets.view", "platform.support.tickets.manage",
            "platform.analytics.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Senior Support Engineer",
        "description": (
            "Escalated support tier with user impersonation rights for advanced "
            "diagnostics. Can escalate tickets and access system logs."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Support)",
            "Platform - Support (Escalate)",
            "Platform - System Logs",
            "Platform - Analytics (Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view", "platform.users.impersonate",
            "platform.support.tickets.view", "platform.support.tickets.manage",
            "platform.support.escalate",
            "platform.system.logs.view",
            "platform.analytics.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Compliance Officer",
        "description": (
            "Read access to audit logs across all tenants. Can export audit logs for "
            "compliance reporting. Can view security alerts and anomaly flags. "
            "Cannot modify any tenant data or configuration."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - Compliance",
            "Platform - Analytics (Read)",
            "Platform - Financial Reports",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.compliance.view", "platform.compliance.manage",
            "platform.audit.logs.view", "platform.audit.logs.export",
            "platform.analytics.view",
            "platform.reports.financial",
            "platform.security.incidents.view",
        ],
    },

    {
        "name": "Security Analyst",
        "description": (
            "Access to security event logs, failed login reports, anomaly flags, and IP "
            "monitoring dashboards. Can trigger account locks and session terminations in "
            "response to security incidents. Cannot access institution data or modify RBAC."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - System Logs",
            "Platform - Audit (Export)",
            "Platform - Security",
            "Platform - Integrations (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.system.config.view",
            "platform.system.logs.view",
            "platform.audit.logs.view", "platform.audit.logs.export",
            "platform.security.incidents.view", "platform.security.incidents.manage",
            "platform.security.pen_test.manage",
            "platform.integrations.view",
            "platform.data.backups.view",
        ],
    },

    {
        "name": "Platform Finance Officer",
        "description": (
            "Manages school billing, subscription plans, invoicing, and financial "
            "reporting at the platform level."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Billing (Full)",
            "Platform - Analytics (Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.billing.view", "platform.billing.manage", "platform.billing.export",
            "platform.reports.financial",
            "platform.analytics.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Onboarding Specialist",
        "description": (
            "Creates and configures new institution tenants. Executes the full onboarding "
            "workflow — tenant provisioning, data import, module configuration, role seeding, "
            "and handoff. Cannot delete tenants or reset production data."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Onboard)",
            "Platform - Users (Read)",
            "Platform - Billing (Read)",
            "Platform - Analytics (Read)",
            "Platform - Support (Tickets Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view", "platform.schools.create", "platform.schools.update",
            "platform.users.view",
            "platform.billing.view",
            "platform.analytics.view",
            "platform.support.tickets.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Frontend Engineer",
        "description": (
            "Access to API documentation, design system tooling, and staging API endpoints. "
            "No institution data access. No admin console access."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [],
        "permissions": [
            "platform.schools.view",
        ],
    },

    {
        "name": "UI/UX Designer",
        "description": (
            "Access to design tooling and staging environment for design review. "
            "No admin console access. No institution data access."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [],
        "permissions": [
            "platform.schools.view",
        ],
    },

    {
        "name": "Operations Manager",
        "description": (
            "Oversight of all onboarding workflows. Can view all tenant statuses, onboarding "
            "progress, import job summaries, and platform health metrics. Can reassign "
            "onboarding tasks. Cannot modify tenant configuration directly."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Users (Read)",
            "Platform - Analytics (Read)",
            "Platform - Support (Tickets Read)",
            "Platform - Audit (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.analytics.view",
            "platform.support.tickets.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Customer Success Manager",
        "description": (
            "Views assigned institution health metrics, usage data, and onboarding progress. "
            "Can raise internal escalation tickets. Read-only — cannot access student or "
            "financial data."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Schools (Read)",
            "Platform - Analytics (Read)",
            "Platform - Support (Tickets Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.analytics.view",
            "platform.support.tickets.view",
        ],
    },

    {
        "name": "Sales Representative",
        "description": (
            "Access to the institution pipeline dashboard — prospect status, onboarding "
            "stage, and conversion metrics. Demo environment access only. "
            "No live institution data access."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Analytics (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.analytics.view",
        ],
    },

    {
        "name": "Partnerships Manager",
        "description": (
            "Same as Sales Representative, plus access to partner-specific configuration "
            "such as reseller accounts and referral tracking. No institution data access."
        ),
        "is_system_role": True,
        "is_locked": False,
        "groups": [
            "Platform - Analytics (Read)",
            "Platform - Billing (Read)",
        ],
        "permissions": [
            "platform.schools.view",
            "platform.analytics.view",
            "platform.billing.view",
        ],
    },
]


# ===========================================================================
# MANAGEMENT COMMAND
# ===========================================================================

class Command(BaseCommand):
    help = (
        "Idempotently seed Permissions, PermissionDependencies, PermissionGroups, "
        "school SchoolRoleTemplates, and PlatformRoleTemplates."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be seeded without touching the database.",
        )
        parser.add_argument(
            "--school-slug",
            type=str,
            default=None,
            help="Only seed school roles for the school with this slug.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        school_slug: str | None = options["school_slug"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes."))

        # Import models here so Django is fully initialised
        from django.utils.text import slugify

        from vs_rbac.models import (
            GroupPermission,
            Permission,
            PermissionAction,
            PermissionDependency,
            PermissionGroup,
            PermissionModule,
            PermissionResource,
            PlatformRoleChangeRequest,
            PlatformRoleGroup,
            PlatformRolePermission,
            PlatformRoleTemplate,
            PlatformUserRoleAssignment,
            SchoolRoleGroup,
            SchoolRolePermission,
            SchoolRoleTemplate,
        )
        from vs_schools.models import School

        # ── 1. Permissions ────────────────────────────────────────────────
        self.stdout.write("\n[1/5] Seeding Permission registry …")
        perm_created = perm_updated = 0

        if not dry_run:
            with transaction.atomic():
                module_cache: dict = {}
                action_cache: dict = {}

                for key, module_key, action, description, sensitivity, is_restricted in PERMISSIONS:
                    resource_name = key[len(module_key) + 1: -len(action) - 1]

                    if module_key not in module_cache:
                        module_obj, _ = PermissionModule.objects.get_or_create(
                            name=module_key,
                            defaults={"description": "", "is_active": True},
                        )
                        module_cache[module_key] = module_obj
                    module_obj = module_cache[module_key]

                    if action not in action_cache:
                        action_obj, _ = PermissionAction.objects.get_or_create(
                            name=action,
                            defaults={"description": "", "is_active": True},
                        )
                        action_cache[action] = action_obj
                    action_obj = action_cache[action]

                    resource_obj, _ = PermissionResource.objects.get_or_create(
                        module=module_obj,
                        name=resource_name,
                        defaults={"description": "", "is_active": True},
                    )

                    _, created = Permission.objects.update_or_create(
                        key=key,
                        defaults=dict(
                            module=module_obj,
                            resource=resource_obj,
                            action=action_obj,
                            description=description,
                            sensitivity_level=sensitivity,
                            is_restricted=is_restricted,
                            is_active=True,
                        ),
                    )
                    if created:
                        perm_created += 1
                    else:
                        perm_updated += 1
        else:
            perm_created = len(PERMISSIONS)

        self.stdout.write(
            self.style.SUCCESS(
                f"  Permissions → {perm_created} created / {perm_updated} updated  "
                f"(total defined: {len(PERMISSIONS)})"
            )
        )

        # ── 2. Permission Dependencies ────────────────────────────────────
        self.stdout.write("\n[2/5] Seeding Permission dependencies …")
        dep_created = dep_updated = 0

        if not dry_run:
            with transaction.atomic():
                for perm_key, depends_on_key in PERMISSION_DEPENDENCIES:
                    _, created = PermissionDependency.objects.update_or_create(
                        permission_id=perm_key,
                        depends_on_id=depends_on_key,
                        defaults={},
                    )
                    if created:
                        dep_created += 1
                    else:
                        dep_updated += 1
        else:
            dep_created = len(PERMISSION_DEPENDENCIES)

        self.stdout.write(
            self.style.SUCCESS(
                f"  PermissionDependencies → {dep_created} created / {dep_updated} updated  "
                f"(total defined: {len(PERMISSION_DEPENDENCIES)})"
            )
        )

        # ── 3. Permission Groups ──────────────────────────────────────────
        self.stdout.write("\n[3/5] Seeding Permission groups …")
        group_created = group_updated = gp_created = gp_updated = gp_deleted = 0

        # Build a name → set(permission_key) lookup; the role-seeding phase
        # below uses this to compute residual direct grants.
        groups_by_name: dict[str, set[str]] = {}

        if not dry_run:
            with transaction.atomic():
                for group_def in PERMISSION_GROUPS:
                    group, created = PermissionGroup.objects.update_or_create(
                        name=group_def["name"],
                        defaults=dict(
                            description=group_def["description"],
                            is_system=True,
                            is_active=True,
                        ),
                    )
                    if created:
                        group_created += 1
                    else:
                        group_updated += 1

                    desired_keys = set(group_def["permissions"])
                    groups_by_name[group.name] = desired_keys

                    existing_keys = set(
                        GroupPermission.objects.filter(group=group).values_list(
                            "permission_id", flat=True
                        )
                    )

                    to_add = desired_keys - existing_keys
                    to_remove = existing_keys - desired_keys

                    if to_remove:
                        deleted, _ = GroupPermission.objects.filter(
                            group=group, permission_id__in=to_remove
                        ).delete()
                        gp_deleted += deleted

                    if to_add:
                        perms = Permission.objects.filter(key__in=to_add)
                        GroupPermission.objects.bulk_create(
                            [
                                GroupPermission(group=group, permission=perm)
                                for perm in perms
                            ]
                        )
                        gp_created += len(to_add)

                    # Count kept memberships as "updated" for reporting parity
                    gp_updated += len(desired_keys & existing_keys)
        else:
            group_created = len(PERMISSION_GROUPS)
            for group_def in PERMISSION_GROUPS:
                groups_by_name[group_def["name"]] = set(group_def["permissions"])

        self.stdout.write(
            self.style.SUCCESS(
                f"  PermissionGroups     → {group_created} created / {group_updated} updated\n"
                f"  GroupPermissions     → {gp_created} created / {gp_updated} kept / {gp_deleted} removed"
            )
        )

        # ── 4. Remove legacy school-less SchoolRoleTemplates ───────────────────
        # School roles are now created per-institution from PrebuiltSchoolRoleTemplate.
        # Any SchoolRoleTemplate with school=None was seeded by the old approach and
        # must be removed so the new structure is the single source of truth.
        self.stdout.write("\n[4/5] Removing legacy school-less SchoolRoleTemplates …")
        removed_count = 0

        if not dry_run:
            with transaction.atomic():
                legacy_qs = SchoolRoleTemplate.objects.filter(school__isnull=True)
                removed_count = legacy_qs.count()
                if removed_count:
                    legacy_qs.delete()
        else:
            removed_count = SchoolRoleTemplate.objects.filter(school__isnull=True).count()

        self.stdout.write(
            self.style.SUCCESS(f"  Removed {removed_count} legacy school-less SchoolRoleTemplate(s).")
        )

        # ── 5. Platform Role Templates ────────────────────────────────────
        self.stdout.write("\n[5/5] Seeding PlatformRoleTemplates …")
        plat_role_created = plat_role_updated = 0
        plat_rp_created = plat_rp_updated = plat_rp_deleted = 0
        plat_rg_created = plat_rg_kept = plat_rg_deleted = 0

        if not dry_run:
            with transaction.atomic():
                for role_def in PLATFORM_ROLES:
                    slug = slugify(role_def["name"])
                    role, created = PlatformRoleTemplate.objects.update_or_create(
                        id=slug,
                        defaults=dict(
                            name=role_def["name"],
                            description=role_def["description"],
                            is_system_role=role_def.get("is_system_role", True),
                            is_locked=role_def.get("is_locked", False),
                            status=PlatformRoleTemplate.Status.ACTIVE,
                        ),
                    )
                    if created:
                        plat_role_created += 1
                    else:
                        plat_role_updated += 1
                        role.bump_version()
                        role.save(update_fields=["version", "updated_at"])

                    # 5a. Sync attached permission groups
                    desired_group_names = role_def.get("groups", [])
                    desired_group_ids = set(
                        PermissionGroup.objects.filter(
                            name__in=desired_group_names
                        ).values_list("id", flat=True)
                    )
                    existing_group_ids = set(
                        PlatformRoleGroup.objects.filter(role=role).values_list(
                            "group_id", flat=True
                        )
                    )

                    to_add_gids = desired_group_ids - existing_group_ids
                    to_remove_gids = existing_group_ids - desired_group_ids

                    if to_remove_gids:
                        deleted, _ = PlatformRoleGroup.objects.filter(
                            role=role, group_id__in=to_remove_gids
                        ).delete()
                        plat_rg_deleted += deleted

                    if to_add_gids:
                        PlatformRoleGroup.objects.bulk_create(
                            [
                                PlatformRoleGroup(role=role, group_id=gid)
                                for gid in to_add_gids
                            ]
                        )
                        plat_rg_created += len(to_add_gids)

                    plat_rg_kept += len(desired_group_ids & existing_group_ids)

                    # 5b. Residual direct permissions
                    role_permission_keys = set(role_def["permissions"])
                    group_covered_keys: set[str] = set()
                    for gname in desired_group_names:
                        group_covered_keys |= groups_by_name.get(gname, set())

                    residual_direct_keys = role_permission_keys - group_covered_keys

                    existing_direct_keys = set(
                        PlatformRolePermission.objects.filter(
                            role=role, granted=True
                        ).values_list("permission_id", flat=True)
                    )

                    to_add_direct = residual_direct_keys - existing_direct_keys
                    to_remove_direct = existing_direct_keys - residual_direct_keys

                    if to_remove_direct:
                        deleted, _ = PlatformRolePermission.objects.filter(
                            role=role, permission_id__in=to_remove_direct
                        ).delete()
                        plat_rp_deleted += deleted

                    if to_add_direct:
                        perms = Permission.objects.filter(key__in=to_add_direct)
                        PlatformRolePermission.objects.bulk_create(
                            [
                                PlatformRolePermission(
                                    role=role,
                                    permission=perm,
                                    granted=True,
                                )
                                for perm in perms
                            ]
                        )
                        plat_rp_created += len(to_add_direct)

                    plat_rp_updated += len(residual_direct_keys & existing_direct_keys)

        else:
            plat_role_created = len(PLATFORM_ROLES)

        self.stdout.write(
            self.style.SUCCESS(
                f"  PlatformRoleTemplates → {plat_role_created} created / {plat_role_updated} updated\n"
                f"  PlatformRolePerms     → {plat_rp_created} created / {plat_rp_updated} kept / {plat_rp_deleted} removed\n"
                f"  PlatformRoleGroups    → {plat_rg_created} attached / {plat_rg_kept} kept / {plat_rg_deleted} detached"
            )
        )

        # ── Summary ───────────────────────────────────────────────────────
        self.stdout.write(
            self.style.SUCCESS(
                f"\n✅  Seeding complete.\n"
                f"    Permissions        : {len(PERMISSIONS)}\n"
                f"    Dependencies       : {len(PERMISSION_DEPENDENCIES)}\n"
                f"    Permission Groups  : {len(PERMISSION_GROUPS)}\n"
                f"    Legacy school roles removed : {removed_count}\n"
                f"    Platform Roles     : {len(PLATFORM_ROLES)}\n"
            )
        )
