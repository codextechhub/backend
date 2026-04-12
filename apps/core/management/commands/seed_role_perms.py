"""
Management command: seed_roles_and_permissions
===============================================
Idempotently seeds:

  1. Permission registry        → Permission
  2. School role templates       → RoleTemplate  (school-scoped)
  3. School role↔permission map  → RolePermission
  4. Platform role templates     → PlatformRoleTemplate  (Vision-owned)
  5. Platform role↔permission    → PlatformRolePermission

Run
---
    python manage.py seed_roles_and_permissions
    python manage.py seed_roles_and_permissions --dry-run
    python manage.py seed_roles_and_permissions --school-slug demo-primary

Notes
-----
* Every write uses update_or_create so the command is safe to re-run.
* --dry-run prints a summary without touching the database.
* School roles are seeded against every active school unless --school-slug
  is supplied.
* Platform roles are global; they are always seeded.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# ---------------------------------------------------------------------------
# ① PERMISSION REGISTRY
#    Format: (key, module_key, action, description, sensitivity, is_restricted)
# ---------------------------------------------------------------------------

PERMISSIONS: list[tuple] = [

    # ── DASHBOARD ──────────────────────────────────────────────────────────
    ("dashboard.overview.view",         "dashboard",    "view",     "View main dashboard overview",                     "NORMAL",    False),
    ("dashboard.analytics.view",        "dashboard",    "view",     "View analytics widgets on dashboard",              "NORMAL",    False),
    ("dashboard.announcements.manage",  "dashboard",    "manage",   "Create and publish school announcements",          "NORMAL",    False),

    # ── STUDENTS ───────────────────────────────────────────────────────────
    ("students.profile.view",           "students",     "view",     "View student profiles",                            "NORMAL",    False),
    ("students.profile.create",         "students",     "create",   "Enrol a new student",                              "NORMAL",    False),
    ("students.profile.update",         "students",     "update",   "Edit student profile details",                     "NORMAL",    False),
    ("students.profile.delete",         "students",     "delete",   "Remove a student record permanently",              "CRITICAL",  True),
    ("students.profile.export",         "students",     "export",   "Export student records to CSV/XLSX",               "SENSITIVE", True),
    ("students.class.assign",           "students",     "assign",   "Assign student to a class/arm",                    "NORMAL",    False),
    ("students.class.transfer",         "students",     "transfer", "Transfer student between classes or branches",     "SENSITIVE", True),
    ("students.disciplinary.view",      "students",     "view",     "View disciplinary records",                        "SENSITIVE", False),
    ("students.disciplinary.manage",    "students",     "manage",   "Add/update disciplinary notes",                    "SENSITIVE", False),
    ("students.medical.view",           "students",     "view",     "View student medical records",                     "CRITICAL",  True),
    ("students.medical.manage",         "students",     "manage",   "Update student medical/health records",            "CRITICAL",  True),
    ("students.guardian.view",          "students",     "view",     "View linked guardian/parent records",              "NORMAL",    False),
    ("students.guardian.manage",        "students",     "manage",   "Link or update guardian records",                  "NORMAL",    False),
    ("students.id_card.generate",       "students",     "generate", "Generate and print student ID cards",              "NORMAL",    False),

    # ── STAFF ──────────────────────────────────────────────────────────────
    ("staff.profile.view",              "staff",        "view",     "View staff profiles",                              "NORMAL",    False),
    ("staff.profile.create",            "staff",        "create",   "Onboard a new staff member",                       "SENSITIVE", False),
    ("staff.profile.update",            "staff",        "update",   "Edit staff profile details",                       "SENSITIVE", False),
    ("staff.profile.delete",            "staff",        "delete",   "Remove a staff record",                            "CRITICAL",  True),
    ("staff.profile.export",            "staff",        "export",   "Export staff records",                             "SENSITIVE", True),
    ("staff.salary.view",               "staff",        "view",     "View salary/payroll entries",                      "CRITICAL",  True),
    ("staff.salary.manage",             "staff",        "manage",   "Create or modify payroll entries",                 "CRITICAL",  True),
    ("staff.leave.apply",               "staff",        "apply",    "Submit a leave request",                           "NORMAL",    False),
    ("staff.leave.approve",             "staff",        "approve",  "Approve or reject staff leave requests",           "SENSITIVE", False),
    ("staff.appraisal.view",            "staff",        "view",     "View staff appraisal records",                     "SENSITIVE", False),
    ("staff.appraisal.manage",          "staff",        "manage",   "Conduct and submit staff appraisals",              "SENSITIVE", False),
    ("staff.attendance.view",           "staff",        "view",     "View staff attendance records",                    "NORMAL",    False),
    ("staff.attendance.mark",           "staff",        "mark",     "Mark or edit staff attendance",                    "NORMAL",    False),

    # ── ACADEMICS ──────────────────────────────────────────────────────────
    ("academics.curriculum.view",       "academics",    "view",     "View curriculum and scheme of work",               "NORMAL",    False),
    ("academics.curriculum.manage",     "academics",    "manage",   "Edit curriculum structures",                       "SENSITIVE", False),
    ("academics.timetable.view",        "academics",    "view",     "View class timetables",                            "NORMAL",    False),
    ("academics.timetable.manage",      "academics",    "manage",   "Create and publish timetables",                    "NORMAL",    False),
    ("academics.subjects.view",         "academics",    "view",     "View subject catalogue",                           "NORMAL",    False),
    ("academics.subjects.manage",       "academics",    "manage",   "Add/edit subjects and assign teachers",            "NORMAL",    False),
    ("academics.class.view",            "academics",    "view",     "View class and arm listings",                      "NORMAL",    False),
    ("academics.class.manage",          "academics",    "manage",   "Create and manage classes/arms",                   "NORMAL",    False),
    ("academics.lesson_notes.view",     "academics",    "view",     "View lesson notes/plans",                          "NORMAL",    False),
    ("academics.lesson_notes.manage",   "academics",    "manage",   "Upload or edit lesson notes",                      "NORMAL",    False),
    ("academics.lesson_notes.approve",  "academics",    "approve",  "Approve lesson notes before publishing",          "NORMAL",    False),

    # ── ASSESSMENTS & RESULTS ──────────────────────────────────────────────
    ("assessments.scores.view",         "assessments",  "view",     "View student assessment scores",                   "NORMAL",    False),
    ("assessments.scores.enter",        "assessments",  "enter",    "Enter CA/test scores for assigned subjects",       "NORMAL",    False),
    ("assessments.scores.edit",         "assessments",  "edit",     "Edit previously submitted scores",                 "SENSITIVE", True),
    ("assessments.scores.approve",      "assessments",  "approve",  "Approve/ratify score sheets",                      "SENSITIVE", False),
    ("assessments.results.publish",     "assessments",  "publish",  "Publish term/semester results to students",        "SENSITIVE", False),
    ("assessments.results.export",      "assessments",  "export",   "Export result sheets",                             "SENSITIVE", True),
    ("assessments.report_card.print",   "assessments",  "print",    "Print student report cards",                       "NORMAL",    False),
    ("assessments.exam_schedule.view",  "assessments",  "view",     "View examination timetable",                       "NORMAL",    False),
    ("assessments.exam_schedule.manage","assessments",  "manage",   "Create and publish exam timetables",               "SENSITIVE", False),
    ("assessments.grading.manage",      "assessments",  "manage",   "Configure grading scales and promotion rules",     "SENSITIVE", True),

    # ── ATTENDANCE ─────────────────────────────────────────────────────────
    ("attendance.student.view",         "attendance",   "view",     "View student attendance records",                  "NORMAL",    False),
    ("attendance.student.mark",         "attendance",   "mark",     "Mark daily/period attendance for students",        "NORMAL",    False),
    ("attendance.student.edit",         "attendance",   "edit",     "Edit previously marked attendance",                "SENSITIVE", True),
    ("attendance.student.export",       "attendance",   "export",   "Export attendance reports",                        "SENSITIVE", True),
    ("attendance.student.report",       "attendance",   "report",   "Generate attendance summary reports",              "NORMAL",    False),

    # ── FINANCE ────────────────────────────────────────────────────────────
    ("finance.fees.view",               "finance",      "view",     "View fee schedules and student fee records",       "SENSITIVE", False),
    ("finance.fees.manage",             "finance",      "manage",   "Create and update fee structures",                 "CRITICAL",  True),
    ("finance.fees.waive",              "finance",      "waive",    "Grant full or partial fee waivers",                "CRITICAL",  True),
    ("finance.invoice.view",            "finance",      "view",     "View student invoices",                            "SENSITIVE", False),
    ("finance.invoice.create",          "finance",      "create",   "Generate invoices for students",                   "SENSITIVE", False),
    ("finance.invoice.approve",         "finance",      "approve",  "Approve invoices before sending",                  "CRITICAL",  True),
    ("finance.payment.record",          "finance",      "record",   "Record manual payment receipts",                   "SENSITIVE", False),
    ("finance.payment.verify",          "finance",      "verify",   "Verify payment proof/receipt uploads",             "SENSITIVE", False),
    ("finance.payment.reverse",         "finance",      "reverse",  "Reverse or void a payment record",                 "CRITICAL",  True),
    ("finance.expenditure.view",        "finance",      "view",     "View school expenditure records",                  "CRITICAL",  True),
    ("finance.expenditure.manage",      "finance",      "manage",   "Record and approve school expenditures",           "CRITICAL",  True),
    ("finance.reports.view",            "finance",      "view",     "View financial summary reports",                   "CRITICAL",  True),
    ("finance.reports.export",          "finance",      "export",   "Export financial reports",                         "CRITICAL",  True),
    ("finance.budget.view",             "finance",      "view",     "View annual school budget",                        "CRITICAL",  True),
    ("finance.budget.manage",           "finance",      "manage",   "Create and approve budget lines",                  "CRITICAL",  True),

    # ── LIBRARY ────────────────────────────────────────────────────────────
    ("library.catalog.view",            "library",      "view",     "View library book catalogue",                      "NORMAL",    False),
    ("library.catalog.manage",          "library",      "manage",   "Add/edit/remove books in catalogue",               "NORMAL",    False),
    ("library.borrow.record",           "library",      "record",   "Issue books to borrowers",                         "NORMAL",    False),
    ("library.borrow.return",           "library",      "return",   "Process book returns",                             "NORMAL",    False),
    ("library.overdue.manage",          "library",      "manage",   "Manage overdue books and fines",                   "NORMAL",    False),
    ("library.reports.view",            "library",      "view",     "View library usage reports",                       "NORMAL",    False),

    # ── HEALTH / MEDICAL ───────────────────────────────────────────────────
    ("health.visits.view",              "health",       "view",     "View student sick-bay visit logs",                 "SENSITIVE", False),
    ("health.visits.record",            "health",       "record",   "Log a student sick-bay visit",                     "SENSITIVE", False),
    ("health.medication.manage",        "health",       "manage",   "Manage medication stock and administration logs",  "CRITICAL",  True),
    ("health.reports.view",             "health",       "view",     "View health/medical summary reports",              "CRITICAL",  True),

    # ── COMMUNICATION ──────────────────────────────────────────────────────
    ("communication.sms.send",          "communication","send",     "Send SMS notifications to parents/staff",          "NORMAL",    False),
    ("communication.email.send",        "communication","send",     "Send email broadcasts",                            "NORMAL",    False),
    ("communication.announcement.post", "communication","post",     "Post announcements on the notice board",           "NORMAL",    False),
    ("communication.chat.view",         "communication","view",     "View internal messaging threads",                  "NORMAL",    False),
    ("communication.chat.send",         "communication","send",     "Send messages in internal chat",                   "NORMAL",    False),

    # ── ADMISSIONS ─────────────────────────────────────────────────────────
    ("admissions.application.view",     "admissions",   "view",     "View incoming admission applications",             "NORMAL",    False),
    ("admissions.application.process",  "admissions",   "process",  "Shortlist, interview and decide on applicants",    "SENSITIVE", False),
    ("admissions.application.approve",  "admissions",   "approve",  "Formally approve or reject applications",          "SENSITIVE", True),
    ("admissions.enrollment.confirm",   "admissions",   "confirm",  "Convert accepted applicant to enrolled student",   "SENSITIVE", False),

    # ── HOSTEL / BOARDING ──────────────────────────────────────────────────
    ("hostel.room.view",                "hostel",       "view",     "View hostel room listings and allocations",        "NORMAL",    False),
    ("hostel.room.manage",              "hostel",       "manage",   "Create/edit rooms and allocate students",          "NORMAL",    False),
    ("hostel.attendance.mark",          "hostel",       "mark",     "Take hostel roll call",                            "NORMAL",    False),
    ("hostel.incident.report",          "hostel",       "report",   "Log hostel incidents",                             "SENSITIVE", False),
    ("hostel.incident.manage",          "hostel",       "manage",   "Resolve and escalate hostel incidents",            "SENSITIVE", False),

    # ── TRANSPORT ──────────────────────────────────────────────────────────
    ("transport.routes.view",           "transport",    "view",     "View transport routes and vehicles",               "NORMAL",    False),
    ("transport.routes.manage",         "transport",    "manage",   "Create/edit routes and assign vehicles",           "NORMAL",    False),
    ("transport.students.assign",       "transport",    "assign",   "Assign students to transport routes",              "NORMAL",    False),
    ("transport.tracking.view",         "transport",    "view",     "View live vehicle/GPS tracking",                   "NORMAL",    False),

    # ── CANTEEN / CAFETERIA ────────────────────────────────────────────────
    ("canteen.menu.view",               "canteen",      "view",     "View canteen menu and prices",                     "NORMAL",    False),
    ("canteen.menu.manage",             "canteen",      "manage",   "Update canteen menu and pricing",                  "NORMAL",    False),
    ("canteen.orders.manage",           "canteen",      "manage",   "Process and fulfil canteen orders",                "NORMAL",    False),
    ("canteen.sales.report",            "canteen",      "report",   "Generate canteen daily/weekly sales reports",      "NORMAL",    False),

    # ── EVENTS ─────────────────────────────────────────────────────────────
    ("events.calendar.view",            "events",       "view",     "View school event calendar",                       "NORMAL",    False),
    ("events.calendar.manage",          "events",       "manage",   "Create/edit events on the school calendar",        "NORMAL",    False),
    ("events.attendance.track",         "events",       "track",    "Track attendance at school events",                "NORMAL",    False),

    # ── ALUMNI ─────────────────────────────────────────────────────────────
    ("alumni.profile.view",             "alumni",       "view",     "View alumni directory",                            "NORMAL",    False),
    ("alumni.profile.manage",           "alumni",       "manage",   "Update alumni records",                            "NORMAL",    False),
    ("alumni.communications.send",      "alumni",       "send",     "Send communications to alumni",                    "NORMAL",    False),

    # ── SETTINGS / CONFIGURATION ───────────────────────────────────────────
    ("settings.school.view",            "settings",     "view",     "View school configuration settings",               "NORMAL",    False),
    ("settings.school.manage",          "settings",     "manage",   "Edit school-wide configuration",                   "CRITICAL",  True),
    ("settings.branch.view",            "settings",     "view",     "View branch configuration",                        "NORMAL",    False),
    ("settings.branch.manage",          "settings",     "manage",   "Edit branch-level configuration",                  "SENSITIVE", True),
    ("settings.academic_session.manage","settings",     "manage",   "Open/close academic sessions and terms",           "CRITICAL",  True),
    ("settings.roles.view",             "settings",     "view",     "View role and permission configurations",          "SENSITIVE", False),
    ("settings.roles.manage",           "settings",     "manage",   "Create/edit school roles and assign permissions",  "CRITICAL",  True),

    # ── REPORTS (cross-module) ─────────────────────────────────────────────
    ("reports.school_wide.view",        "reports",      "view",     "Access school-wide consolidated reports",          "SENSITIVE", False),
    ("reports.school_wide.export",      "reports",      "export",   "Export school-wide consolidated reports",          "SENSITIVE", True),

    # ── AUDIT LOG ──────────────────────────────────────────────────────────
    ("audit.logs.view",                 "audit",        "view",     "View system audit trail",                          "CRITICAL",  True),
    ("audit.logs.export",               "audit",        "export",   "Export audit logs",                                "CRITICAL",  True),

    # ══════════════════════════════════════════════════════════════════════
    # PLATFORM-ONLY PERMISSIONS (Vision internal — used by PlatformRoleTemplate)
    # ══════════════════════════════════════════════════════════════════════

    # ── PLATFORM: SCHOOL MANAGEMENT ────────────────────────────────────────
    ("platform.schools.view",           "platform",     "view",     "View all schools on the platform",                 "SENSITIVE", False),
    ("platform.schools.create",         "platform",     "create",   "Onboard a new school onto the platform",           "CRITICAL",  True),
    ("platform.schools.update",         "platform",     "update",   "Update school profile and configuration",          "CRITICAL",  True),
    ("platform.schools.suspend",        "platform",     "suspend",  "Suspend or reactivate a school account",           "CRITICAL",  True),
    ("platform.schools.delete",         "platform",     "delete",   "Permanently delete a school record",               "CRITICAL",  True),

    # ── PLATFORM: BILLING / SUBSCRIPTIONS ─────────────────────────────────
    ("platform.billing.view",           "platform",     "view",     "View school billing and subscription records",     "CRITICAL",  True),
    ("platform.billing.manage",         "platform",     "manage",   "Edit plans, issue credits, manage invoices",       "CRITICAL",  True),
    ("platform.billing.export",         "platform",     "export",   "Export billing data",                              "CRITICAL",  True),

    # ── PLATFORM: USER MANAGEMENT ─────────────────────────────────────────
    ("platform.users.view",             "platform",     "view",     "View all platform and school user accounts",       "SENSITIVE", False),
    ("platform.users.impersonate",      "platform",     "impersonate","Impersonate a school user for support",          "CRITICAL",  True),
    ("platform.users.suspend",          "platform",     "suspend",  "Suspend or reactivate user accounts",              "CRITICAL",  True),
    ("platform.users.delete",           "platform",     "delete",   "Permanently delete user accounts",                 "CRITICAL",  True),

    # ── PLATFORM: ROLES & PERMISSIONS ─────────────────────────────────────
    ("platform.roles.view",             "platform",     "view",     "View platform role templates and permissions",     "SENSITIVE", False),
    ("platform.roles.manage",           "platform",     "manage",   "Create/edit platform role templates",              "CRITICAL",  True),
    ("platform.permissions.manage",     "platform",     "manage",   "Add/remove entries in the permission registry",    "CRITICAL",  True),

    # ── PLATFORM: SUPPORT ─────────────────────────────────────────────────
    ("platform.support.tickets.view",   "platform",     "view",     "View support tickets from all schools",            "SENSITIVE", False),
    ("platform.support.tickets.manage", "platform",     "manage",   "Respond to and resolve support tickets",           "SENSITIVE", False),
    ("platform.support.escalate",       "platform",     "escalate", "Escalate a ticket to engineering or management",   "SENSITIVE", False),

    # ── PLATFORM: ANALYTICS & REPORTING ───────────────────────────────────
    ("platform.analytics.view",         "platform",     "view",     "View platform-wide analytics dashboards",          "SENSITIVE", False),
    ("platform.analytics.export",       "platform",     "export",   "Export platform analytics datasets",               "CRITICAL",  True),
    ("platform.reports.financial",      "platform",     "view",     "View platform-wide financial reports",             "CRITICAL",  True),

    # ── PLATFORM: SYSTEM / INFRA ───────────────────────────────────────────
    ("platform.system.config.view",     "platform",     "view",     "View platform system configuration",               "CRITICAL",  True),
    ("platform.system.config.manage",   "platform",     "manage",   "Edit platform-level system settings",              "CRITICAL",  True),
    ("platform.system.deployments.view","platform",     "view",     "View deployment history and release notes",        "SENSITIVE", False),
    ("platform.system.deployments.trigger","platform",  "trigger",  "Trigger deployments or rollbacks",                 "CRITICAL",  True),
    ("platform.system.logs.view",       "platform",     "view",     "View system/application logs",                     "CRITICAL",  True),
    ("platform.system.maintenance.manage","platform",   "manage",   "Enable/disable maintenance mode globally",         "CRITICAL",  True),
    ("platform.integrations.view",      "platform",     "view",     "View third-party integration configurations",      "SENSITIVE", False),
    ("platform.integrations.manage",    "platform",     "manage",   "Add/edit/revoke platform integrations",            "CRITICAL",  True),

    # ── PLATFORM: COMPLIANCE & AUDIT ──────────────────────────────────────
    ("platform.compliance.view",        "platform",     "view",     "View compliance frameworks and checklists",        "CRITICAL",  True),
    ("platform.compliance.manage",      "platform",     "manage",   "Manage compliance records and evidence",           "CRITICAL",  True),
    ("platform.audit.logs.view",        "platform",     "view",     "View platform-wide audit log",                     "CRITICAL",  True),
    ("platform.audit.logs.export",      "platform",     "export",   "Export platform audit logs",                       "CRITICAL",  True),

    # ── PLATFORM: DATA ENGINEERING ────────────────────────────────────────
    ("platform.data.pipelines.view",    "platform",     "view",     "View data pipeline statuses",                      "SENSITIVE", False),
    ("platform.data.pipelines.manage",  "platform",     "manage",   "Create/edit/trigger data pipelines",               "CRITICAL",  True),
    ("platform.data.migrations.run",    "platform",     "run",      "Execute schema and data migrations",               "CRITICAL",  True),
    ("platform.data.backups.view",      "platform",     "view",     "View backup schedules and restore points",         "CRITICAL",  True),
    ("platform.data.backups.manage",    "platform",     "manage",   "Create, schedule, and restore backups",            "CRITICAL",  True),

    # ── PLATFORM: SECURITY ────────────────────────────────────────────────
    ("platform.security.incidents.view","platform",     "view",     "View security incident records",                   "CRITICAL",  True),
    ("platform.security.incidents.manage","platform",   "manage",   "Investigate and resolve security incidents",       "CRITICAL",  True),
    ("platform.security.pen_test.manage","platform",    "manage",   "Manage penetration test plans and findings",       "CRITICAL",  True),
]


# ---------------------------------------------------------------------------
# ② SCHOOL ROLE TEMPLATES
#    Format: (slug_name, description, is_system_role, permissions_list)
#    permissions_list → list of permission keys granted to this role
# ---------------------------------------------------------------------------

SCHOOL_ROLES: list[dict] = [

    # ── LEADERSHIP ──────────────────────────────────────────────────────────
    {
        "name": "School Principal",
        "description": (
            "Top administrator responsible for all academic and operational decisions. "
            "Has full visibility across students, staff, finance, and settings. "
            "Applicable globally as Head Teacher (UK), Headmaster/Headmistress, "
            "Principal (US/NG/IN), Director (international schools)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view", "dashboard.analytics.view", "dashboard.announcements.manage",
            "students.profile.view", "students.profile.create", "students.profile.update",
            "students.profile.export", "students.class.assign", "students.class.transfer",
            "students.disciplinary.view", "students.disciplinary.manage",
            "students.medical.view", "students.guardian.view", "students.guardian.manage",
            "students.id_card.generate",
            "staff.profile.view", "staff.profile.create", "staff.profile.update",
            "staff.profile.export", "staff.salary.view",
            "staff.leave.approve", "staff.appraisal.view", "staff.appraisal.manage",
            "staff.attendance.view", "staff.attendance.mark",
            "academics.curriculum.view", "academics.curriculum.manage",
            "academics.timetable.view", "academics.timetable.manage",
            "academics.subjects.view", "academics.subjects.manage",
            "academics.class.view", "academics.class.manage",
            "academics.lesson_notes.view", "academics.lesson_notes.approve",
            "assessments.scores.view", "assessments.scores.approve",
            "assessments.results.publish", "assessments.results.export",
            "assessments.report_card.print", "assessments.exam_schedule.view",
            "assessments.exam_schedule.manage",
            "attendance.student.view", "attendance.student.report", "attendance.student.export",
            "finance.fees.view", "finance.invoice.view",
            "finance.payment.record", "finance.payment.verify",
            "finance.expenditure.view", "finance.reports.view", "finance.reports.export",
            "finance.budget.view",
            "library.catalog.view", "library.reports.view",
            "health.visits.view", "health.reports.view",
            "communication.sms.send", "communication.email.send",
            "communication.announcement.post", "communication.chat.view", "communication.chat.send",
            "admissions.application.view", "admissions.application.process",
            "admissions.application.approve", "admissions.enrollment.confirm",
            "hostel.room.view", "hostel.incident.manage",
            "transport.routes.view", "transport.students.assign", "transport.tracking.view",
            "events.calendar.view", "events.calendar.manage",
            "alumni.profile.view",
            "settings.school.view", "settings.branch.view",
            "settings.academic_session.manage", "settings.roles.view",
            "reports.school_wide.view", "reports.school_wide.export",
            "audit.logs.view",
        ],
    },

    {
        "name": "Vice Principal (Academics)",
        "description": (
            "Deputy head with primary oversight of academic programmes, curriculum, "
            "timetabling, assessments, and teacher performance. "
            "Known as Deputy Head Teacher (UK), Vice Principal Academics (NG/US/IN)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view", "dashboard.analytics.view",
            "students.profile.view", "students.class.assign", "students.class.transfer",
            "students.disciplinary.view", "students.disciplinary.manage",
            "staff.profile.view", "staff.appraisal.view", "staff.appraisal.manage",
            "staff.attendance.view",
            "academics.curriculum.view", "academics.curriculum.manage",
            "academics.timetable.view", "academics.timetable.manage",
            "academics.subjects.view", "academics.subjects.manage",
            "academics.class.view", "academics.class.manage",
            "academics.lesson_notes.view", "academics.lesson_notes.manage",
            "academics.lesson_notes.approve",
            "assessments.scores.view", "assessments.scores.approve",
            "assessments.results.publish", "assessments.results.export",
            "assessments.report_card.print",
            "assessments.exam_schedule.view", "assessments.exam_schedule.manage",
            "assessments.grading.manage",
            "attendance.student.view", "attendance.student.report",
            "communication.announcement.post", "communication.chat.view", "communication.chat.send",
            "events.calendar.view", "events.calendar.manage",
            "reports.school_wide.view",
        ],
    },

    {
        "name": "Vice Principal (Administration)",
        "description": (
            "Deputy head overseeing non-academic operations: staff welfare, discipline, "
            "logistics, events, and general school administration."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.disciplinary.view", "students.disciplinary.manage",
            "students.guardian.view", "students.guardian.manage",
            "staff.profile.view", "staff.profile.update",
            "staff.leave.approve", "staff.attendance.view", "staff.attendance.mark",
            "academics.timetable.view", "academics.class.view",
            "attendance.student.view", "attendance.student.mark",
            "attendance.student.report",
            "communication.sms.send", "communication.email.send",
            "communication.announcement.post", "communication.chat.view", "communication.chat.send",
            "hostel.room.view", "hostel.room.manage", "hostel.attendance.mark",
            "hostel.incident.report", "hostel.incident.manage",
            "transport.routes.view", "transport.students.assign",
            "events.calendar.view", "events.calendar.manage", "events.attendance.track",
            "settings.school.view", "settings.branch.view",
            "reports.school_wide.view",
        ],
    },

    # ── TEACHING STAFF ──────────────────────────────────────────────────────
    {
        "name": "Class Teacher",
        "description": (
            "Primary teacher assigned to a specific class/form. Responsible for daily "
            "attendance, welfare, report card comments, and pastoral care of students "
            "in their class. Known as Form Teacher (NG/GH/KE), Homeroom Teacher (US), "
            "Form Tutor (UK)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.disciplinary.view", "students.disciplinary.manage",
            "students.guardian.view", "students.id_card.generate",
            "academics.timetable.view", "academics.subjects.view", "academics.class.view",
            "academics.lesson_notes.view", "academics.lesson_notes.manage",
            "assessments.scores.view", "assessments.scores.enter",
            "assessments.report_card.print", "assessments.exam_schedule.view",
            "attendance.student.view", "attendance.student.mark", "attendance.student.report",
            "communication.chat.view", "communication.chat.send",
            "events.calendar.view",
        ],
    },

    {
        "name": "Subject Teacher",
        "description": (
            "Teacher responsible for delivering a specific subject across one or more "
            "classes. Enters scores, uploads lesson notes, and views student records "
            "for their assigned subjects only."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "academics.timetable.view", "academics.subjects.view", "academics.class.view",
            "academics.lesson_notes.view", "academics.lesson_notes.manage",
            "assessments.scores.view", "assessments.scores.enter",
            "assessments.exam_schedule.view",
            "attendance.student.view", "attendance.student.mark",
            "communication.chat.view", "communication.chat.send",
            "events.calendar.view",
        ],
    },

    {
        "name": "Head of Department",
        "description": (
            "Senior teacher managing a subject department (e.g., Sciences, Languages, "
            "Arts). Approves lesson notes, reviews scores, and coordinates curriculum "
            "for their department. Known as HOD worldwide."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view", "dashboard.analytics.view",
            "students.profile.view",
            "staff.profile.view", "staff.appraisal.view",
            "academics.curriculum.view", "academics.curriculum.manage",
            "academics.timetable.view", "academics.subjects.view", "academics.subjects.manage",
            "academics.class.view",
            "academics.lesson_notes.view", "academics.lesson_notes.manage",
            "academics.lesson_notes.approve",
            "assessments.scores.view", "assessments.scores.enter", "assessments.scores.approve",
            "assessments.exam_schedule.view", "assessments.exam_schedule.manage",
            "attendance.student.view", "attendance.student.report",
            "communication.chat.view", "communication.chat.send",
            "events.calendar.view",
            "reports.school_wide.view",
        ],
    },

    {
        "name": "Exam Officer",
        "description": (
            "Coordinates examinations: scheduling, score collection, result processing, "
            "and report card generation. Has elevated access to assessment modules. "
            "Known as Exams Officer (UK/NG/GH) or Registrar (US/tertiary contexts)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.id_card.generate",
            "academics.class.view", "academics.subjects.view", "academics.timetable.view",
            "assessments.scores.view", "assessments.scores.enter", "assessments.scores.edit",
            "assessments.scores.approve", "assessments.results.publish",
            "assessments.results.export", "assessments.report_card.print",
            "assessments.exam_schedule.view", "assessments.exam_schedule.manage",
            "assessments.grading.manage",
            "communication.sms.send", "communication.announcement.post",
            "communication.chat.view", "communication.chat.send",
            "reports.school_wide.view", "reports.school_wide.export",
        ],
    },

    {
        "name": "Guidance Counsellor",
        "description": (
            "Provides pastoral support and counselling to students. Accesses disciplinary, "
            "academic, and welfare records to support student development. Known as "
            "School Counsellor (US/CA), Pastoral Care Officer (UK/AU)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.disciplinary.view",
            "students.medical.view", "students.guardian.view",
            "academics.timetable.view",
            "assessments.scores.view",
            "attendance.student.view", "attendance.student.report",
            "health.visits.view",
            "communication.chat.view", "communication.chat.send",
            "events.calendar.view",
        ],
    },

    # ── ADMINISTRATIVE STAFF ────────────────────────────────────────────────
    {
        "name": "School Secretary",
        "description": (
            "Front-office administrator handling correspondence, admissions paperwork, "
            "scheduling, and general school records. Known as Administrative Assistant "
            "or Registrar in various systems."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.profile.create", "students.profile.update",
            "students.guardian.view", "students.guardian.manage", "students.id_card.generate",
            "staff.profile.view",
            "academics.timetable.view", "academics.class.view",
            "attendance.student.view",
            "communication.sms.send", "communication.email.send",
            "communication.announcement.post", "communication.chat.view", "communication.chat.send",
            "admissions.application.view", "admissions.application.process",
            "admissions.enrollment.confirm",
            "events.calendar.view", "events.calendar.manage",
            "alumni.profile.view",
        ],
    },

    {
        "name": "Admissions Officer",
        "description": (
            "Manages the full admissions pipeline from application receipt through to "
            "enrolment confirmation. Common in secondary, tertiary, and international schools."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.profile.create", "students.profile.update",
            "students.guardian.view", "students.guardian.manage", "students.id_card.generate",
            "academics.class.view",
            "communication.sms.send", "communication.email.send",
            "communication.chat.view", "communication.chat.send",
            "admissions.application.view", "admissions.application.process",
            "admissions.application.approve", "admissions.enrollment.confirm",
            "reports.school_wide.view",
        ],
    },

    # ── FINANCE ─────────────────────────────────────────────────────────────
    {
        "name": "School Bursar",
        "description": (
            "Chief financial officer of the school. Manages fee structures, invoicing, "
            "payment collection, expenditure, and financial reporting. "
            "Known as Bursar (NG/UK), Finance Officer (US/AU/international schools)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view", "dashboard.analytics.view",
            "students.profile.view",
            "finance.fees.view", "finance.fees.manage", "finance.fees.waive",
            "finance.invoice.view", "finance.invoice.create", "finance.invoice.approve",
            "finance.payment.record", "finance.payment.verify", "finance.payment.reverse",
            "finance.expenditure.view", "finance.expenditure.manage",
            "finance.reports.view", "finance.reports.export",
            "finance.budget.view", "finance.budget.manage",
            "communication.sms.send", "communication.email.send",
            "communication.chat.view", "communication.chat.send",
            "reports.school_wide.view", "reports.school_wide.export",
        ],
    },

    {
        "name": "Finance Clerk",
        "description": (
            "Assists the Bursar with day-to-day payment collection, receipt recording, "
            "and basic financial data entry. Has no approval authority."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "finance.fees.view",
            "finance.invoice.view", "finance.invoice.create",
            "finance.payment.record", "finance.payment.verify",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── HEALTH ──────────────────────────────────────────────────────────────
    {
        "name": "School Nurse",
        "description": (
            "Healthcare professional managing the school sick bay. Records student "
            "visits, administers first aid, tracks medication, and generates health "
            "reports. Known as School Health Officer in some African contexts."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.medical.view", "students.medical.manage",
            "students.guardian.view",
            "health.visits.view", "health.visits.record",
            "health.medication.manage", "health.reports.view",
            "communication.chat.view", "communication.chat.send",
            "communication.sms.send",
        ],
    },

    # ── LIBRARY ─────────────────────────────────────────────────────────────
    {
        "name": "Librarian",
        "description": (
            "Manages the school library: cataloguing books, processing loans/returns, "
            "handling overdue books, and generating usage reports."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "library.catalog.view", "library.catalog.manage",
            "library.borrow.record", "library.borrow.return",
            "library.overdue.manage", "library.reports.view",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── IT ───────────────────────────────────────────────────────────────────
    {
        "name": "School IT Administrator",
        "description": (
            "Manages the school's technology infrastructure and the XVS platform at the "
            "school level. Configures roles, imports data, and monitors system health. "
            "Does not have access to confidential student or financial data."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view", "dashboard.analytics.view",
            "settings.school.view", "settings.branch.view",
            "settings.roles.view", "settings.roles.manage",
            "audit.logs.view",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── HOSTEL / BOARDING ────────────────────────────────────────────────────
    {
        "name": "House Master / House Mistress",
        "description": (
            "Boarding staff member responsible for a residential house/dorm. Conducts "
            "roll calls, manages incidents, and oversees student welfare in the hostel. "
            "Known as Dorm Supervisor (US/CA), House Parent (international schools)."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view", "students.disciplinary.view", "students.disciplinary.manage",
            "students.medical.view", "students.guardian.view",
            "health.visits.view",
            "hostel.room.view", "hostel.room.manage",
            "hostel.attendance.mark", "hostel.incident.report", "hostel.incident.manage",
            "communication.chat.view", "communication.chat.send", "communication.sms.send",
            "events.calendar.view",
        ],
    },

    # ── TRANSPORT ────────────────────────────────────────────────────────────
    {
        "name": "Transport Coordinator",
        "description": (
            "Manages bus routes, vehicle assignments, and student transport allocations. "
            "Tracks GPS and handles transport-related communications with parents."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "transport.routes.view", "transport.routes.manage",
            "transport.students.assign", "transport.tracking.view",
            "communication.sms.send", "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── CANTEEN ──────────────────────────────────────────────────────────────
    {
        "name": "Canteen Manager",
        "description": (
            "Oversees daily canteen operations including menu management, order processing, "
            "and sales reporting. Known as Cafeteria Manager (US) or Dining Hall Supervisor."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "canteen.menu.view", "canteen.menu.manage",
            "canteen.orders.manage", "canteen.sales.report",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── PARENT / GUARDIAN ────────────────────────────────────────────────────
    {
        "name": "Parent / Guardian",
        "description": (
            "Read-only access for parents and guardians linked to enrolled students. "
            "Can view their ward's profile, results, attendance, fee status, and "
            "communicate with the school. Portal access role used worldwide."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "academics.timetable.view",
            "assessments.scores.view", "assessments.report_card.print",
            "assessments.exam_schedule.view",
            "attendance.student.view",
            "finance.fees.view", "finance.invoice.view",
            "library.catalog.view",
            "transport.routes.view", "transport.tracking.view",
            "events.calendar.view",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── STUDENT ──────────────────────────────────────────────────────────────
    {
        "name": "Student",
        "description": (
            "Self-service portal access for enrolled students. Can view their own "
            "profile, results, timetable, attendance, and communicate within the platform."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "academics.timetable.view", "academics.subjects.view",
            "academics.lesson_notes.view",
            "assessments.scores.view", "assessments.report_card.print",
            "assessments.exam_schedule.view",
            "attendance.student.view",
            "finance.fees.view", "finance.invoice.view",
            "library.catalog.view",
            "transport.routes.view",
            "events.calendar.view",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── ALUMNI ───────────────────────────────────────────────────────────────
    {
        "name": "Alumni Member",
        "description": (
            "Graduated student with access to the alumni network, directory, and "
            "school communications. Limited read-only access."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "alumni.profile.view",
            "events.calendar.view",
            "communication.chat.view", "communication.chat.send",
        ],
    },

    # ── SECURITY / GATE ─────────────────────────────────────────────────────
    {
        "name": "Security Officer",
        "description": (
            "Gate and premises security staff. Can verify student/staff identity "
            "and log entry/exit events. No access to academic or financial data."
        ),
        "is_system_role": True,
        "permissions": [
            "dashboard.overview.view",
            "students.profile.view",
            "staff.profile.view",
            "transport.routes.view", "transport.tracking.view",
            "communication.chat.view",
        ],
    },
]


# ---------------------------------------------------------------------------
# ③ PLATFORM ROLE TEMPLATES (Vision-owned / tech-org context)
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
        "permissions": [p[0] for p in PERMISSIONS if p[0].startswith("platform.")],
        # Gets ALL platform permissions
    },

    {
        "name": "Platform Engineering Lead",
        "description": (
            "Senior engineer with access to system config, deployments, data pipelines, "
            "logs, and integration management. No billing or school deletion access."
        ),
        "is_system_role": True,
        "is_locked": False,
        "permissions": [
            "platform.schools.view",
            "platform.users.view",
            "platform.roles.view",
            "platform.analytics.view",
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
        "name": "DevOps / Infrastructure Engineer",
        "description": (
            "Responsible for CI/CD pipelines, deployment infrastructure, database "
            "operations, and backup management on the platform. "
            "No access to school-level data or billing."
        ),
        "is_system_role": True,
        "is_locked": False,
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
        "name": "QA / Test Engineer",
        "description": (
            "Runs quality assurance and testing cycles across the platform. "
            "Read access to configurations and logs. No write access to production data."
        ),
        "is_system_role": True,
        "is_locked": False,
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
        "name": "Data Analyst",
        "description": (
            "Analyses school and platform data to produce insights and reports. "
            "Read-only access to analytics, pipelines, and audit logs. "
            "Can export datasets for analysis."
        ),
        "is_system_role": True,
        "is_locked": False,
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
        "name": "Data Engineer",
        "description": (
            "Builds and maintains data pipelines, ETL processes, and the data "
            "warehouse that powers platform analytics and reporting."
        ),
        "is_system_role": True,
        "is_locked": False,
        "permissions": [
            "platform.schools.view",
            "platform.analytics.view",
            "platform.data.pipelines.view", "platform.data.pipelines.manage",
            "platform.data.migrations.run",
            "platform.data.backups.view", "platform.data.backups.manage",
            "platform.system.logs.view",
            "platform.integrations.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "Customer Support Officer",
        "description": (
            "First-line support agent responding to school tickets. Can view school "
            "profiles and user accounts for diagnostics. No impersonation or deletion rights."
        ),
        "is_system_role": True,
        "is_locked": False,
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
        "name": "Compliance Reviewer",
        "description": (
            "Responsible for data protection, regulatory audits, and compliance "
            "framework management. Read access across compliance records and audit logs. "
            "No system config or deployment access."
        ),
        "is_system_role": True,
        "is_locked": False,
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
        "name": "Security Engineer",
        "description": (
            "Platform security specialist managing incident response, vulnerability "
            "assessments, and penetration testing coordination."
        ),
        "is_system_role": True,
        "is_locked": False,
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
        "permissions": [
            "platform.schools.view",
            "platform.billing.view", "platform.billing.manage", "platform.billing.export",
            "platform.reports.financial",
            "platform.analytics.view",
            "platform.audit.logs.view",
        ],
    },

    {
        "name": "School Onboarding Specialist",
        "description": (
            "Creates and configures new school accounts on the platform. "
            "Handles initial data import, branch setup, and account activation. "
            "No deletion or suspension rights."
        ),
        "is_system_role": True,
        "is_locked": False,
        "permissions": [
            "platform.schools.view", "platform.schools.create", "platform.schools.update",
            "platform.users.view",
            "platform.billing.view",
            "platform.analytics.view",
            "platform.support.tickets.view",
            "platform.audit.logs.view",
        ],
    },
]


# ===========================================================================
# MANAGEMENT COMMAND
# ===========================================================================

class Command(BaseCommand):
    help = "Idempotently seed Permissions, school RoleTemplates, and PlatformRoleTemplates."

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
        from vs_rbac.models import (
            Permission,
            RoleTemplate,
            RolePermission,
            PlatformRoleTemplate,
            PlatformRolePermission,
        )
        from vs_schools.models import School

        # ── 1. Permissions ────────────────────────────────────────────────
        self.stdout.write("\n[1/3] Seeding Permission registry …")
        perm_created = perm_updated = 0

        if not dry_run:
            with transaction.atomic():
                for key, module_key, action, description, sensitivity, is_restricted in PERMISSIONS:
                    _, created = Permission.objects.update_or_create(
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

        # ── 2. School Role Templates ──────────────────────────────────────
        self.stdout.write("\n[2/3] Seeding School RoleTemplates …")

        schools = School.objects.filter(status="ACTIVE")
        if school_slug:
            schools = schools.filter(slug=school_slug)
            if not schools.exists():
                raise CommandError(f"No active school found with slug '{school_slug}'.")

        school_role_created = school_role_updated = school_rp_created = school_rp_updated = 0

        if not dry_run:
            with transaction.atomic():
                for school in schools:
                    for role_def in SCHOOL_ROLES:
                        role, created = RoleTemplate.objects.update_or_create(
                            school=school,
                            name=role_def["name"],
                            defaults=dict(
                                description=role_def["description"],
                                is_system_role=role_def.get("is_system_role", True),
                                status=RoleTemplate.Status.ACTIVE,
                            ),
                        )
                        if created:
                            school_role_created += 1
                            if not created:
                                role.bump_version()
                                role.save(update_fields=["version"])
                        else:
                            school_role_updated += 1

                        # Sync permissions
                        for perm_key in role_def["permissions"]:
                            rp, rp_created = RolePermission.objects.update_or_create(
                                role=role,
                                permission_id=perm_key,
                                defaults=dict(granted=True),
                            )
                            if rp_created:
                                school_rp_created += 1
                            else:
                                school_rp_updated += 1
        else:
            school_role_created = len(SCHOOL_ROLES) * schools.count()
            school_rp_created = sum(len(r["permissions"]) for r in SCHOOL_ROLES) * schools.count()

        self.stdout.write(
            self.style.SUCCESS(
                f"  School RoleTemplates → {school_role_created} created / {school_role_updated} updated\n"
                f"  RolePermissions     → {school_rp_created} created / {school_rp_updated} updated"
            )
        )

        # ── 3. Platform Role Templates ────────────────────────────────────
        self.stdout.write("\n[3/3] Seeding PlatformRoleTemplates …")
        plat_role_created = plat_role_updated = plat_rp_created = plat_rp_updated = 0

        if not dry_run:
            with transaction.atomic():
                for role_def in PLATFORM_ROLES:
                    role, created = PlatformRoleTemplate.objects.update_or_create(
                        name=role_def["name"],
                        defaults=dict(
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
                        role.save(update_fields=["version"])

                    for perm_key in role_def["permissions"]:
                        rp, rp_created = PlatformRolePermission.objects.update_or_create(
                            role=role,
                            permission_id=perm_key,
                            defaults=dict(granted=True),
                        )
                        if rp_created:
                            plat_rp_created += 1
                        else:
                            plat_rp_updated += 1
        else:
            plat_role_created = len(PLATFORM_ROLES)
            plat_rp_created = sum(len(r["permissions"]) for r in PLATFORM_ROLES)

        self.stdout.write(
            self.style.SUCCESS(
                f"  PlatformRoleTemplates → {plat_role_created} created / {plat_role_updated} updated\n"
                f"  PlatformRolePerms     → {plat_rp_created} created / {plat_rp_updated} updated"
            )
        )

        # ── Summary ───────────────────────────────────────────────────────
        self.stdout.write(
            self.style.SUCCESS(
                f"\n✅  Seeding complete.\n"
                f"    Permissions        : {len(PERMISSIONS)}\n"
                f"    School Roles       : {len(SCHOOL_ROLES)} (per school)\n"
                f"    Platform Roles     : {len(PLATFORM_ROLES)}\n"
            )
        )