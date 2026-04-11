"""
Management command: seed_permissions

Seeds all Permission records and PermissionDependency records across all
27 CodeX Vision modules (505 permissions, ~500+ dependencies).

Usage:
    python manage.py seed_permissions
    python manage.py seed_permissions --dry-run
    python manage.py seed_permissions --reset   # Clears and re-seeds (use with caution)

Design rules:
- Fully idempotent: safe to run multiple times without side effects
- Uses get_or_create for permissions so existing records are preserved
- Uses bulk_create(ignore_conflicts=True) for dependencies for performance
- Structured by Tier → Module to mirror the FRD document
- All permission keys follow the module.resource.action format
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# ---------------------------------------------------------------------------
# Permission catalog
# Each entry: (key, module_key, action, capability)
# ---------------------------------------------------------------------------

PERMISSIONS: list[tuple[str, str, str, str]] = [

    # ==========================================================================
    # GLOBAL PLATFORM GUARDS
    # ==========================================================================
    ("system.session.access.authenticate",      "system", "authenticate",  "Allows a signed-in user session to be established before any protected workflow is used."),
    ("system.authenticated.access",             "system", "access",        "Allows access only after the request is authenticated."),
    ("system.api.access",                       "system", "access",        "Allows access to protected API endpoints when transport, auth, and client checks pass."),
    ("system.tenant_context.require",           "system", "require",       "Requires every tenant-scoped request to carry a valid school context."),
    ("system.tenant_boundary.enforce",          "system", "enforce",       "Prevents cross-school reads, writes, or reference leakage."),
    ("system.mfa.enforce",                      "system", "enforce",       "Requires multi-factor authentication for privileged or sensitive access."),
    ("system.mfa.reset",                        "system", "reset",         "Allows an authorized operator to reset a user MFA enrollment."),
    ("system.password.policy.enforce",          "system", "enforce",       "Requires passwords to satisfy the configured complexity and rotation policy."),
    ("system.token.refresh",                    "system", "refresh",       "Allows a refresh token to mint a new access token under policy."),
    ("system.rate_limit.login.enforce",         "system", "enforce",       "Applies login throttling after repeated failed authentication attempts."),
    ("system.session.view",                     "system", "view",          "Allows viewing active sessions, devices, IP data, and last activity."),
    ("system.session.force_logout",             "system", "force",         "Allows a privileged actor to revoke sessions and sign a user out."),
    ("system.audit.write",                      "system", "write",         "Requires sensitive state changes to emit an immutable audit record."),
    ("system.audit.view",                       "system", "view",          "Allows authorized users to inspect the audit trail."),
    ("system.audit.search",                     "system", "search",        "Allows searching audit data by actor, date, entity, or event type."),
    ("system.audit.export",                     "system", "export",        "Allows export of audit results to approved formats under policy."),
    ("system.configuration.view",               "system", "view",          "Allows viewing platform and school configuration values."),
    ("system.configuration.update",             "system", "update",        "Allows changing configuration values after validation and audit checks."),
    ("system.feature_flag.view",                "system", "view",          "Allows reading current feature flag state and rollout scope."),
    ("system.feature_flag.toggle",              "system", "toggle",        "Allows enabling or disabling a feature flag."),
    ("system.feature_flag.schedule",            "system", "schedule",      "Allows scheduling future activation or deactivation of a flag."),
    ("system.health.view",                      "system", "view",          "Allows access to platform health, provisioning, and usage indicators."),
    ("system.security_alert.view",              "system", "view",          "Allows inspection of failed-auth, anomaly, and security alert events."),
    ("system.support.impersonation.impersonate","system", "impersonate",   "Allows audited user impersonation for support and troubleshooting."),
    ("system.download.secure_link.generate",    "system", "generate",      "Allows generation of expiring secure download links for exports."),
    ("system.data_masking.apply",               "system", "apply",         "Allows masking of sensitive fields in exports, reports, and previews."),
    ("system.export.schedule",                  "system", "schedule",      "Allows automated recurring export jobs to be configured."),
    ("system.export.expiry.enforce",            "system", "enforce",       "Allows export files to be expired and revoked after policy windows."),
    ("system.role_request.approve",             "system", "approve",       "Allows privileged staff to approve role change requests."),
    ("system.role_request.deny",                "system", "deny",          "Allows privileged staff to deny role change requests."),
    ("system.approval.override.super_admin",    "system", "override",      "Allows Super Admin override on exceptional guarded operations."),

    # ==========================================================================
    # TIER 1 — MODULE 1: SCHOOL & BRANCH MANAGEMENT
    # ==========================================================================
    ("school.school.create",              "school", "create",       "Create School alongside Subsidiaries."),
    ("school.school_slug.generate",       "school", "generate",     "Auto-Generate School Slug."),
    ("school.database_schema.provision",       "school", "provision",    "Provision School Database Schema."),
    ("school.localization.assign",             "school", "assign",       "Assign School Region and Localization."),
    ("school.lifecycle.configure",             "school", "configure",    "Configure School Lifecycle States."),
    ("school.module_access.toggle",            "school", "toggle",       "Enable / Disable Modules Per School."),
    ("school.primary_administrator.assign",    "school", "assign",       "Assign Primary School Administrator."),
    ("school.branding_assets.upload",          "school", "upload",       "Upload School Branding Assets."),
    ("school.metadata.update",                 "school", "update",       "Update School Metadata."),
    ("school.uniqueness.validate",             "school", "validate",     "Validate School Uniqueness."),
    ("school.data_isolation.enforce",          "school", "enforce",      "Enforce School Data Isolation."),
    ("school.access_state.suspend",            "school", "suspend",      "Suspend School."),
    ("school.access_state.reactivate",         "school", "reactivate",   "Reactivate Suspended School."),
    ("school.deletion_state.soft_delete",      "school", "soft_delete",  "Soft Delete School."),
    ("school.deletion_state.hard_delete",      "school", "hard_delete",  "Hard Delete School."),
    ("school.configuration.reset",             "school", "reset",        "Reset School Configuration."),
    ("school.health_status.view",              "school", "view",         "View School Health Status."),
    ("school.creation_audit_logs.track",       "school", "track",        "Track School Creation Audit Logs."),
    ("school.feature_flags.enforce",           "school", "enforce",      "Enforce School-Level Feature Flags."),
    ("school.provisioning.rollback",           "school", "rollback",     "Rollback Failed School Provisioning."),

    # ==========================================================================
    # TIER 1 — MODULE 2: VISION ADMIN CONSOLE
    # ==========================================================================
    ("vision_admin.session.authenticate",                       "vision_admin", "authenticate",  "Authenticate Vision Admin User."),
    ("vision_admin.school_dashboard.view",                 "vision_admin", "view",          "View All Schools Dashboard."),
    ("vision_admin.school_configuration.edit",             "vision_admin", "edit",          "Edit School Configuration."),
    ("vision_admin.access_state.suspend_manage",                "vision_admin", "suspend_manage","Suspend / Unsuspend School."),
    ("vision_admin.data_configuration.reset",                   "vision_admin", "reset",         "Reset School Data or Configuration."),
    ("vision_admin.school_provisioning_pipeline.monitor",  "vision_admin", "monitor",       "Monitor School Provisioning Pipeline."),
    ("vision_admin.failed_provisioning_steps.retry",            "vision_admin", "retry",         "Retry Failed Provisioning Steps."),
    ("vision_admin.school_usage_metrics.view",             "vision_admin", "view",          "View School Usage Metrics."),
    ("vision_admin.school_audit_logs.access",              "vision_admin", "access",        "Access School Audit Logs."),
    ("vision_admin.school_user.impersonate",               "vision_admin", "impersonate",   "Impersonate School User."),
    ("vision_admin.role_change_request.manage",                 "vision_admin", "manage",        "Manage Role Change Requests."),
    ("vision_admin.import_jobs.review",                         "vision_admin", "review",        "Review Import Jobs and Errors."),
    ("vision_admin.data_fix.apply",                             "vision_admin", "apply",         "Apply Manual Data Fixes."),
    ("vision_admin.feature_flags.toggle",                       "vision_admin", "toggle",        "Toggle Feature Flags Per School."),
    ("vision_admin.health_metrics.view",                        "vision_admin", "view",          "View System Health Metrics."),
    ("vision_admin.admin_roles.manage",                         "vision_admin", "manage",        "Manage Vision Admin Roles."),
    ("vision_admin.mfa.enforce",                                "vision_admin", "enforce",       "Enforce MFA for Vision Staff."),
    ("vision_admin.security_alerts.view",                       "vision_admin", "view",          "View Security Alerts."),
    ("vision_admin.admin_actions.log",                          "vision_admin", "log",           "Log All Admin Actions."),

    # ==========================================================================
    # TIER 1 — MODULE 3: USER IDENTITY, ACCOUNTS & AUTHENTICATION
    # ==========================================================================
    ("identity.user_account.create",                "identity", "create",   "Create User Account."),
    ("identity.user_email.invite",                  "identity", "invite",   "Invite User via Email."),
    ("identity.user_account.activate",              "identity", "activate", "Activate User Account."),
    ("identity.password_policy.enforce",            "identity", "enforce",  "Enforce Password Policy."),
    ("identity.access_token.refresh",               "identity", "refresh",  "Refresh Access Token."),
    ("identity.user_account.lock",                  "identity", "lock",     "Lock User Account."),
    ("identity.user_account.unlock",                "identity", "unlock",   "Unlock User Account."),
    ("identity.login_sessions.track",               "identity", "track",    "Track Login Sessions."),
    ("identity.user_logout.force",                  "identity", "force",    "Force User Logout."),
    ("identity.user_password.reset",                "identity", "reset",    "Reset User Password."),
    ("identity.email_address.verify",               "identity", "verify",   "Verify Email Address."),
    ("identity.phone_number.verify",                "identity", "verify",   "Verify Phone Number."),
    ("identity.school_aware_login.enforce",    "identity", "enforce",  "Enforce School-Aware Login."),
    ("identity.authentication_events.log",          "identity", "log",      "Log Authentication Events."),

    # ==========================================================================
    # TIER 1 — MODULE 4: ROLES, PERMISSIONS & ACCESS CONTROL
    # ==========================================================================
    ("rbac.role_template.create",               "rbac", "create",     "Create Role Template."),
    ("rbac.role_user.assign",                   "rbac", "assign",     "Assign Role to User."),
    ("rbac.role_permissions.update",            "rbac", "update",     "Update Role Permissions."),
    ("rbac.role_based_access_control.enforce",  "rbac", "enforce",    "Enforce Role-Based Access Control."),
    ("rbac.cross_module_access.restrict",       "rbac", "restrict",   "Restrict Cross-Module Access."),
    ("rbac.permission_dependencies.validate",   "rbac", "validate",   "Validate Permission Dependencies."),
    ("rbac.permission_changes_instantly.apply", "rbac", "apply",      "Apply Permission Changes Instantly."),
    ("rbac.role_change.request",                "rbac", "request",    "Request Role Change."),
    ("rbac.role_change.approve",                "rbac", "approve",    "Approve Role Change."),
    ("rbac.role_change_request.deny",           "rbac", "deny",       "Deny Role Change Request."),
    ("rbac.multiple_roles_user.assign",         "rbac", "assign",     "Assign Multiple Roles to User."),
    ("rbac.role.deactivate",                    "rbac", "deactivate", "Deactivate Role."),
    ("rbac.role_template.archive",              "rbac", "archive",    "Archive Role Template."),
    ("rbac.api_level_permission_checks.enforce","rbac", "enforce",    "Enforce API-Level Permission Checks."),
    ("rbac.role_changes.audit",                 "rbac", "audit",      "Audit Role Changes."),
    ("rbac.compliance_constraints.enforce",     "rbac", "enforce",    "Enforce Compliance Constraints."),
    ("rbac.permission_matrix.view",             "rbac", "view",       "View Permission Matrix."),
    ("rbac.previous_role_state.restore",        "rbac", "restore",    "Restore Previous Role State."),
    ("rbac.critical_system_roles.lock",         "rbac", "lock",       "Lock Critical System Roles."),

    # ==========================================================================
    # TIER 1 — MODULE 5: AUDIT LOGGING & COMPLIANCE
    # ==========================================================================
    ("audit.user_creation_events.log",      "audit", "log",      "Log User Creation Events."),
    ("audit.authentication_events.log",     "audit", "log",      "Log Authentication Events."),
    ("audit.data_import_actions.log",       "audit", "log",      "Log Data Import Actions."),
    ("audit.configuration_changes.log",     "audit", "log",      "Log Configuration Changes."),
    ("audit.financial_transactions.log",    "audit", "log",      "Log Financial Transactions."),
    ("audit.procurement_actions.log",       "audit", "log",      "Log Procurement Actions."),
    ("audit.permission_changes.log",        "audit", "log",      "Log Permission Changes."),
    ("audit.immutable_log_storage.enforce", "audit", "enforce",  "Enforce Immutable Log Storage."),
    ("audit.audit_events.timestamp",        "audit", "timestamp","Timestamp All Audit Events."),
    ("audit.actions_actor.attribute",       "audit", "attribute","Attribute Actions to Actor."),
    ("audit.audit_logs.search",             "audit", "search",   "Search Audit Logs."),
    ("audit.logs_by_action_type.filter",    "audit", "filter",   "Filter Logs by Action Type."),
    ("audit.logs_by_date_range.filter",     "audit", "filter",   "Filter Logs by Date Range."),
    ("audit.audit_logs.export",             "audit", "export",   "Export Audit Logs."),
    ("audit.log_retrieval.paginate",        "audit", "paginate", "Paginate Log Retrieval."),
    ("audit.audit_trail_entity.view",       "audit", "view",     "Display Audit Trail per Entity."),

    # ==========================================================================
    # TIER 1 — MODULE 6: SYSTEM CONFIGURATION & FEATURE FLAGS
    # ==========================================================================
    ("system_config.global_system_settings.view",                   "system_config", "view",     "View Global System Settings."),
    ("system_config.global_system_settings.update",                 "system_config", "update",   "Update Global System Settings."),
    ("system_config.configuration_key.create",                      "system_config", "create",   "Create Configuration Key."),
    ("system_config.configuration_key.edit",                        "system_config", "edit",     "Edit Configuration Key."),
    ("system_config.configuration_key.delete",                      "system_config", "delete",   "Delete Configuration Key."),
    ("system_config.deleted_configuration_key.restore",             "system_config", "restore",  "Restore Deleted Configuration Key."),
    ("system_config.configuration_schema.validate",                 "system_config", "validate", "Validate Configuration Schema."),
    ("system_config.configuration_access_permissions.enforce",      "system_config", "enforce",  "Enforce Configuration Access Permissions."),
    ("system_config.configuration_change_history.track",            "system_config", "track",    "Track Configuration Change History."),
    ("system_config.configuration_changes.rollback",                "system_config", "rollback", "Rollback Configuration Changes."),
    ("system_config.feature_flag.create",                           "system_config", "create",   "Create Feature Flag."),
    ("system_config.feature_flag_globally.enable",                  "system_config", "enable",   "Enable Feature Flag Globally."),
    ("system_config.feature_flag_globally.disable",                 "system_config", "disable",  "Disable Feature Flag Globally."),
    ("system_config.feature_flag_school.enable",               "system_config", "enable",   "Enable Feature Flag Per School."),
    ("system_config.feature_flag_school.disable",              "system_config", "disable",  "Disable Feature Flag Per School."),
    ("system_config.feature_flag_activation.schedule",              "system_config", "schedule", "Schedule Feature Flag Activation."),
    ("system_config.feature_flag_deactivation.schedule",            "system_config", "schedule", "Schedule Feature Flag Deactivation."),
    ("system_config.flags_specific_roles.restrict",                 "system_config", "restrict", "Restrict Flags to Specific Roles."),
    ("system_config.feature_flag_changes.audit",                    "system_config", "audit",    "Audit Feature Flag Changes."),
    ("system_config.conflicting_flag_rules.detect",                 "system_config", "detect",   "Detect Conflicting Flag Rules."),
    ("system_config.configuration_and_feature_flag_settings.export","system_config", "export",   "Export Configuration and Feature Flag Settings."),

    # ==========================================================================
    # TIER 2 — MODULE 7: NOTIFICATION & MESSAGING ENGINE
    # ==========================================================================
    ("communication.internal_message.send",         "communication", "send",      "Send Internal Message."),
    ("communication.bulk_notifications.send",        "communication", "send",      "Send Bulk Notifications."),
    ("communication.email_notifications.send",       "communication", "send",      "Send Email Notifications."),
    ("communication.sms_notifications.send",         "communication", "send",      "Send SMS Notifications."),
    ("communication.notification_templates.configure","communication","configure", "Configure Notification Templates."),
    ("communication.files_messages.attach",          "communication", "attach",    "Attach Files to Messages."),
    ("communication.message_delivery.track",         "communication", "track",     "Track Message Delivery."),
    ("communication.message_history.view",           "communication", "view",      "View Message History."),
    ("communication.messages_by_type.filter",        "communication", "filter",    "Filter Messages by Type."),
    ("communication.messages.reply",                 "communication", "reply",     "Reply to Messages."),
    ("communication.notification_threads.mute",      "communication", "mute",      "Mute Notification Threads."),
    ("communication.notifications.schedule",         "communication", "schedule",  "Schedule Notifications."),
    ("communication.communication_permissions.enforce","communication","enforce",  "Enforce Communication Permissions."),
    ("communication.communication_events.log",       "communication", "log",       "Log Communication Events."),
    ("communication.message_activity.audit",         "communication", "audit",     "Audit Message Activity."),

    # ==========================================================================
    # TIER 2 — MODULE 8: SCHOOL ONBOARDING
    # ==========================================================================
    ("onboarding.school_onboarding.initiate",      "onboarding", "initiate", "Initiate School Onboarding."),
    ("onboarding.school_metadata.capture",          "onboarding", "capture",  "Capture School Metadata."),
    ("onboarding.academic_structure.configure",          "onboarding", "configure","Configure Academic Structure."),
    ("onboarding.financial_structure.configure",         "onboarding", "configure","Configure Financial Structure."),
    ("onboarding.procurement_structure.configure",       "onboarding", "configure","Configure Procurement Structure."),
    ("onboarding.branch_structure.configure",            "onboarding", "configure","Configure Branch Structure."),
    ("onboarding.default_roles.assign",                  "onboarding", "assign",   "Assign Default Roles."),
    ("onboarding.default_permissions.seed",              "onboarding", "seed",     "Seed Default Permissions."),
    ("onboarding.initial_datasets.upload",               "onboarding", "upload",   "Upload Initial Datasets."),
    ("onboarding.onboarding_completeness.validate",      "onboarding", "validate", "Validate Onboarding Completeness."),
    ("onboarding.onboarding_progress.track",             "onboarding", "track",    "Track Onboarding Progress."),
    ("onboarding.go_live_on_validation_errors.block",    "onboarding", "block",    "Block Go-Live on Validation Errors."),
    ("onboarding.training_session.schedule",             "onboarding", "schedule", "Schedule Training Session."),
    ("onboarding.onboarding_checklist.generate",         "onboarding", "generate", "Generate Onboarding Checklist."),
    ("onboarding.onboarding_actions.log",                "onboarding", "log",      "Log Onboarding Actions."),
    ("onboarding.incomplete_onboarding.rollback",        "onboarding", "rollback", "Rollback Incomplete Onboarding."),
    ("onboarding.post_go_live_activity.monitor",         "onboarding", "monitor",  "Monitor Post-Go-Live Activity."),
    ("onboarding.onboarding_issues.escalate",            "onboarding", "escalate", "Escalate Onboarding Issues."),

    # ==========================================================================
    # TIER 2 — MODULE 9: DATA IMPORT & VALIDATION ENGINE
    # ==========================================================================
    ("data_import.csv_file.upload",                 "data_import", "upload",   "Upload CSV File."),
    ("data_import.excel_file.upload",               "data_import", "upload",   "Upload Excel File."),
    ("data_import.dataset_type.detect",             "data_import", "detect",   "Detect Dataset Type."),
    ("data_import.file_structure.validate",         "data_import", "validate", "Validate File Structure."),
    ("data_import.mandatory_fields.validate",       "data_import", "validate", "Validate Mandatory Fields."),
    ("data_import.duplicate_records.detect",        "data_import", "detect",   "Detect Duplicate Records."),
    ("data_import.cross_entity_references.validate","data_import", "validate", "Validate Cross-Entity References."),
    ("data_import.auto_mapping_rules.apply",        "data_import", "apply",    "Apply Auto-Mapping Rules."),
    ("data_import.field_mapping.map",               "data_import", "map",      "Allow Manual Field Mapping."),
    ("data_import.validation_error_report.generate","data_import", "generate", "Generate Validation Error Report."),
    ("data_import.errors_vs_warnings.classify",     "data_import", "classify", "Classify Errors vs Warnings."),
    ("data_import.import_on_critical_errors.block", "data_import", "block",    "Block Import on Critical Errors."),
    ("data_import.background_import_job.execute",   "data_import", "execute",  "Execute Background Import Job."),
    ("data_import.import_progress.track",           "data_import", "track",    "Track Import Progress."),
    ("data_import.error_import_rows.edit",          "data_import", "edit",     "Edit Error Import Rows."),
    ("data_import.failed_imports.rollback",         "data_import", "rollback", "Rollback Failed Imports."),
    ("data_import.data_changes.log",                "data_import", "log",      "Log Data Changes."),
    ("data_import.large_dataset_imports.support",   "data_import", "support",  "Support Large Dataset Imports."),
    ("data_import.import_history.store",            "data_import", "store",    "Store Import History."),
    ("data_import.admin_on_completion.notify",      "data_import", "notify",   "Notify Admin on Completion."),

    # ==========================================================================
    # TIER 3 — MODULE 10: STUDENT MANAGEMENT
    # ==========================================================================
    ("student.student_profile.create",              "student", "create",   "Create Student Profile."),
    ("student.student_records.update",              "student", "update",   "Update Student Records."),
    ("student.student_class.assign",                "student", "assign",   "Assign Student to Class."),
    ("student.student_between_terms_sessions.promote","student","promote", "Promote Student Between Terms or Sessions."),
    ("student.student_records.archive",             "student", "archive",  "Archive Student Records."),
    ("student.archived_students.restore",           "student", "restore",  "Restore Archived Students."),
    ("student.student_status.manage",               "student", "manage",   "Manage Student Status."),
    ("student.student_documents.upload",            "student", "upload",   "Upload Student Documents."),
    ("student.student_identity.validate",           "student", "validate", "Validate Student Identity."),
    ("student.enrollment_history.track",            "student", "track",    "Track Enrollment History."),
    ("student.parent_guardian_information.add",     "student", "add",      "Add Parent/Guardian Information."),
    ("student.data_privacy_rules.enforce",          "student", "enforce",  "Enforce Data Privacy Rules."),
    ("student.student_records.search",              "student", "search",   "Search Student Records."),
    ("student.student_data.export",                 "student", "export",   "Export Student Data."),
    ("student.student_record_changes.log",          "student", "log",      "Log Student Record Changes."),

    # ==========================================================================
    # TIER 3 — MODULE 11: STAFF MANAGEMENT
    # ==========================================================================
    ("staff.staff_profile.create",      "staff", "create",     "Create Staff Profile."),
    ("staff.staff_role.assign",         "staff", "assign",     "Assign Staff Role."),
    ("staff.staff_department.assign",   "staff", "assign",     "Assign Staff Department."),
    ("staff.staff_account.activate",    "staff", "activate",   "Activate Staff Account."),
    ("staff.staff_account.deactivate",  "staff", "deactivate", "Deactivate Staff Account."),
    ("staff.staff_department.transfer", "staff", "transfer",   "Transfer Staff Department."),
    ("staff.staff_activity.track",      "staff", "track",      "Track Staff Activity."),
    ("staff.staff_permissions.manage",  "staff", "manage",     "Manage Staff Permissions."),
    ("staff.staff_documents.upload",    "staff", "upload",     "Upload Staff Documents."),
    ("staff.staff_records.search",      "staff", "search",     "Search Staff Records."),
    ("staff.staff_data.export",         "staff", "export",     "Export Staff Data."),
    ("staff.staff_actions.log",         "staff", "log",        "Log Staff Actions."),
    ("staff.role_constraints.enforce",  "staff", "enforce",    "Enforce Role Constraints."),
    ("staff.staff_account.restore",     "staff", "restore",    "Restore Staff Account."),
    ("staff.staff_record.archive",      "staff", "archive",    "Archive Staff Record."),

    # ==========================================================================
    # TIER 3 — MODULE 12: ACADEMIC STRUCTURE
    # ==========================================================================
    ("academic_structure.academic_program.create",      "academic_structure", "create",   "Create Academic Program."),
    ("academic_structure.department.create",            "academic_structure", "create",   "Create Department."),
    ("academic_structure.class.create",                 "academic_structure", "create",   "Create Class."),
    ("academic_structure.class_program.assign",         "academic_structure", "assign",   "Assign Class to Program."),
    ("academic_structure.teacher_class.assign",         "academic_structure", "assign",   "Assign Teacher to Class."),
    ("academic_structure.term_structure.define",        "academic_structure", "define",   "Define Term Structure."),
    ("academic_structure.academic_calendar.update",     "academic_structure", "update",   "Update Academic Calendar."),
    ("academic_structure.academic_structure.archive",   "academic_structure", "archive",  "Archive Academic Structure."),
    ("academic_structure.academic_structures.clone",    "academic_structure", "clone",    "Clone Academic Structures."),
    ("academic_structure.structure_dependencies.validate","academic_structure","validate","Validate Structure Dependencies."),
    ("academic_structure.academic_configuration.lock",  "academic_structure", "lock",     "Lock Academic Configuration."),
    ("academic_structure.academic_year.rollover",       "academic_structure", "rollover", "Roll Over Academic Year."),
    ("academic_structure.structural_changes.track",     "academic_structure", "track",    "Track Structural Changes."),
    ("academic_structure.academic_setup.export",        "academic_structure", "export",   "Export Academic Setup."),
    ("academic_structure.academic_modifications.audit", "academic_structure", "audit",    "Audit Academic Modifications."),

    # ==========================================================================
    # TIER 3 — MODULE 13: ACADEMIC CALENDAR & TIMETABLES
    # ==========================================================================
    ("academic_calendar.academic_session.create",       "academic_calendar", "create",    "Create Academic Session."),
    ("academic_calendar.academic_terms.define",         "academic_calendar", "define",    "Define Academic Terms."),
    ("academic_calendar.school_calendar.configure",     "academic_calendar", "configure", "Configure School Calendar."),
    ("academic_calendar.holidays_and_breaks.set",       "academic_calendar", "set",       "Set Holidays and Breaks."),
    ("academic_calendar.class_timetable.create",        "academic_calendar", "create",    "Create Class Timetable."),
    ("academic_calendar.subjects_time_slots.assign",    "academic_calendar", "assign",    "Assign Subjects to Time Slots."),
    ("academic_calendar.teachers_timetable.assign",     "academic_calendar", "assign",    "Assign Teachers to Timetable."),
    ("academic_calendar.scheduling_conflicts.prevent",  "academic_calendar", "prevent",   "Prevent Scheduling Conflicts."),
    ("academic_calendar.timetable.publish",             "academic_calendar", "publish",   "Publish Timetable."),
    ("academic_calendar.timetable.update",              "academic_calendar", "update",    "Update Timetable."),
    ("academic_calendar.users_schedule_changes.notify", "academic_calendar", "notify",    "Notify Users of Schedule Changes."),
    ("academic_calendar.academic_calendar.lock",        "academic_calendar", "lock",      "Lock Academic Calendar."),
    ("academic_calendar.academic_calendar.clone",       "academic_calendar", "clone",     "Clone Academic Calendar."),
    ("academic_calendar.calendar_new_year.rollover",    "academic_calendar", "rollover",  "Roll Over Calendar to New Year."),
    ("academic_calendar.daily_schedule.view",           "academic_calendar", "view",      "Display Daily Schedule."),
    ("academic_calendar.calendar_attendance.sync",      "academic_calendar", "sync",      "Sync Calendar with Attendance."),
    ("academic_calendar.timetable.export",              "academic_calendar", "export",    "Export Timetable."),
    ("academic_calendar.calendar_changes.track",        "academic_calendar", "track",     "Track Calendar Changes."),
    ("academic_calendar.role_based_editing.enforce",    "academic_calendar", "enforce",   "Enforce Role-Based Editing."),
    ("academic_calendar.calendar_updates.audit",        "academic_calendar", "audit",     "Audit Calendar Updates."),

    # ==========================================================================
    # TIER 3 — MODULE 14: ATTENDANCE MANAGEMENT
    # ==========================================================================
    ("attendance.attendance_rules.configure",           "attendance", "configure",  "Configure Attendance Rules."),
    ("attendance.attendance_types.define",              "attendance", "define",     "Define Attendance Types."),
    ("attendance.class_attendance_roster.load",         "attendance", "load",       "Load Class Attendance Roster."),
    ("attendance.student_attendance.record",            "attendance", "record",     "Record Student Attendance."),
    ("attendance.attendance_records.edit",              "attendance", "edit",       "Edit Attendance Records."),
    ("attendance.duplicate_attendance_entries.prevent", "attendance", "prevent",    "Prevent Duplicate Attendance Entries."),
    ("attendance.unrecorded_attendance.auto_mark",      "attendance", "auto_mark",  "Auto-Mark Unrecorded Attendance."),
    ("attendance.attendance_student_profile.sync",      "attendance", "sync",       "Sync Attendance to Student Profile."),
    ("attendance.parents_absence.notify",               "attendance", "notify",     "Notify Parents of Absence."),
    ("attendance.attendance_summary.generate",          "attendance", "generate",   "Generate Attendance Summary."),
    ("attendance.attendance_anomalies.detect",          "attendance", "detect",     "Detect Attendance Anomalies."),
    ("attendance.attendance_records.lock",              "attendance", "lock",       "Lock Attendance Records."),
    ("attendance.locked_attendance.reopen",             "attendance", "reopen",     "Reopen Locked Attendance."),
    ("attendance.attendance_history.track",             "attendance", "track",      "Track Attendance History."),
    ("attendance.attendance_reports.export",            "attendance", "export",     "Export Attendance Reports."),
    ("attendance.role_based_attendance_access.enforce", "attendance", "enforce",    "Enforce Role-Based Attendance Access."),
    ("attendance.attendance_changes.audit",             "attendance", "audit",      "Audit Attendance Changes."),
    ("attendance.bulk_attendance_updates.support",      "attendance", "support",    "Support Bulk Attendance Updates."),

    # ==========================================================================
    # TIER 3 — MODULE 15: GRADEBOOK & ASSESSMENTS
    # ==========================================================================
    ("gradebook.assessment_type.create",        "gradebook", "create",    "Create Assessment Type."),
    ("gradebook.grading_scheme.configure",      "gradebook", "configure", "Configure Grading Scheme."),
    ("gradebook.assessments_classes.assign",    "gradebook", "assign",    "Assign Assessments to Classes."),
    ("gradebook.student_scores.enter",          "gradebook", "enter",     "Enter Student Scores."),
    ("gradebook.submitted_scores.edit",         "gradebook", "edit",      "Edit Submitted Scores."),
    ("gradebook.totals.calculate",              "gradebook", "calculate", "Auto-Calculate Totals."),
    ("gradebook.weighted_grading_rules.apply",  "gradebook", "apply",     "Apply Weighted Grading Rules."),
    ("gradebook.score_ranges.validate",         "gradebook", "validate",  "Validate Score Ranges."),
    ("gradebook.gradebook.lock",                "gradebook", "lock",      "Lock Gradebook."),
    ("gradebook.gradebook.unlock",              "gradebook", "unlock",    "Unlock Gradebook."),
    ("gradebook.report_cards.generate",         "gradebook", "generate",  "Generate Report Cards."),
    ("gradebook.term_results.approve",          "gradebook", "approve",   "Approve Term Results."),
    ("gradebook.results_students.publish",      "gradebook", "publish",   "Publish Results to Students."),
    ("gradebook.parents_results.notify",        "gradebook", "notify",    "Notify Parents of Results."),
    ("gradebook.grade_history.track",           "gradebook", "track",     "Track Grade History."),
    ("gradebook.missing_grades.detect",         "gradebook", "detect",    "Detect Missing Grades."),
    ("gradebook.grade_reports.export",          "gradebook", "export",    "Export Grade Reports."),
    ("gradebook.continuous_assessment.support", "gradebook", "support",   "Support Continuous Assessment."),
    ("gradebook.exam_assessments.support",      "gradebook", "support",   "Support Exam Assessments."),
    ("gradebook.grade_modifications.audit",     "gradebook", "audit",     "Audit Grade Modifications."),

    # ==========================================================================
    # TIER 4 — MODULE 16: BILLING & FEES MANAGEMENT
    # ==========================================================================
    ("finance.billing.fee_item.create",             "finance.billing", "create",    "Create Fee Item."),
    ("finance.billing.fee_structure.configure",     "finance.billing", "configure", "Configure Fee Structure."),
    ("finance.billing.fees_classes.assign",         "finance.billing", "assign",    "Assign Fees to Classes."),
    ("finance.billing.fees_students.assign",        "finance.billing", "assign",    "Assign Fees to Students."),
    ("finance.billing.student_invoices.generate",   "finance.billing", "generate",  "Generate Student Invoices."),
    ("finance.billing.bulk_invoices.generate",      "finance.billing", "generate",  "Generate Bulk Invoices."),
    ("finance.billing.invoice_items.edit",          "finance.billing", "edit",      "Edit Invoice Items."),
    ("finance.billing.late_fee_rules.apply",        "finance.billing", "apply",     "Apply Late Fee Rules."),
    ("finance.billing.discounts.apply",             "finance.billing", "apply",     "Apply Discounts."),
    ("finance.billing.scholarships.apply",          "finance.billing", "apply",     "Apply Scholarships."),
    ("finance.billing.payment_deadlines.configure", "finance.billing", "configure", "Configure Payment Deadlines."),
    ("finance.billing.invoice_status.track",        "finance.billing", "track",     "Track Invoice Status."),
    ("finance.billing.issued_invoices.lock",        "finance.billing", "lock",      "Lock Issued Invoices."),
    ("finance.billing.invoices.cancel",             "finance.billing", "cancel",    "Cancel Invoices."),
    ("finance.billing.invoices.reissue",            "finance.billing", "reissue",   "Reissue Invoices."),
    ("finance.billing.payers_invoices.notify",      "finance.billing", "notify",    "Notify Payers of Invoices."),
    ("finance.billing.fee_history.track",           "finance.billing", "track",     "Track Fee History."),
    ("finance.billing.billing_reports.export",      "finance.billing", "export",    "Export Billing Reports."),
    ("finance.billing.billing_permissions.enforce", "finance.billing", "enforce",   "Enforce Billing Permissions."),
    ("finance.billing.billing_actions.audit",       "finance.billing", "audit",     "Audit Billing Actions."),

    # ==========================================================================
    # TIER 4 — MODULE 17: PAYMENTS & RECONCILIATION
    # ==========================================================================
    ("finance.payment.payment_gateways.configure",      "finance.payment", "configure", "Configure Payment Gateways."),
    ("finance.payment.payment_channels.enable",         "finance.payment", "enable",    "Enable Payment Channels."),
    ("finance.payment.online_payments.process",         "finance.payment", "process",   "Process Online Payments."),
    ("finance.payment.offline_payments.record",         "finance.payment", "record",    "Record Offline Payments."),
    ("finance.payment.payment_receipts.generate",       "finance.payment", "generate",  "Generate Payment Receipts."),
    ("finance.payment.payments_invoices.match",         "finance.payment", "match",     "Match Payments to Invoices."),
    ("finance.payment.partial_payments.handle",         "finance.payment", "handle",    "Handle Partial Payments."),
    ("finance.payment.overpayments.handle",             "finance.payment", "handle",    "Handle Overpayments."),
    ("finance.payment.failed_payments.detect",          "finance.payment", "detect",    "Detect Failed Payments."),
    ("finance.payment.failed_payments.retry",           "finance.payment", "retry",     "Retry Failed Payments."),
    ("finance.payment.gateway_transactions.reconcile",  "finance.payment", "reconcile", "Reconcile Gateway Transactions."),
    ("finance.payment.bank_transfers.reconcile",        "finance.payment", "reconcile", "Reconcile Bank Transfers."),
    ("finance.payment.refund_requests.process",         "finance.payment", "process",   "Process Refund Requests."),
    ("finance.payment.refunds.approve",                 "finance.payment", "approve",   "Approve Refunds."),
    ("finance.payment.refunds.execute",                 "finance.payment", "execute",   "Execute Refunds."),
    ("finance.payment.payment_status.track",            "finance.payment", "track",     "Track Payment Status."),
    ("finance.payment.users_payment_events.notify",     "finance.payment", "notify",    "Notify Users of Payment Events."),
    ("finance.payment.payment_history.export",          "finance.payment", "export",    "Export Payment History."),
    ("finance.payment.payment_approvals.enforce",       "finance.payment", "enforce",   "Enforce Payment Approvals."),
    ("finance.payment.payment_transactions.audit",      "finance.payment", "audit",     "Audit Payment Transactions."),

    # ==========================================================================
    # TIER 4 — MODULE 18: FINANCE LEDGER & REPORTING
    # ==========================================================================
    ("finance.ledger.chart_accounts.configure",     "finance.ledger", "configure", "Configure Chart of Accounts."),
    ("finance.ledger.ledger_accounts.create",       "finance.ledger", "create",    "Create Ledger Accounts."),
    ("finance.ledger.financial_transactions.record","finance.ledger", "record",    "Record Financial Transactions."),
    ("finance.ledger.billing_entries.post",         "finance.ledger", "post",      "Auto-Post Billing Entries."),
    ("finance.ledger.payment_entries.post",         "finance.ledger", "post",      "Auto-Post Payment Entries."),
    ("finance.ledger.expense_entries.record",       "finance.ledger", "record",    "Record Expense Entries."),
    ("finance.ledger.trial_balance.generate",       "finance.ledger", "generate",  "Generate Trial Balance."),
    ("finance.ledger.income_statement.generate",    "finance.ledger", "generate",  "Generate Income Statement."),
    ("finance.ledger.balance_sheet.generate",       "finance.ledger", "generate",  "Generate Balance Sheet."),
    ("finance.ledger.cash_flow_report.generate",    "finance.ledger", "generate",  "Generate Cash Flow Report."),
    ("finance.ledger.outstanding_balances.track",   "finance.ledger", "track",     "Track Outstanding Balances."),
    ("finance.ledger.financial_reports.filter",     "finance.ledger", "filter",    "Filter Financial Reports."),
    ("finance.ledger.financial_statements.export",  "finance.ledger", "export",    "Export Financial Statements."),
    ("finance.ledger.financial_periods.lock",       "finance.ledger", "lock",      "Lock Financial Periods."),
    ("finance.ledger.financial_periods.reopen",     "finance.ledger", "reopen",    "Reopen Financial Periods."),
    ("finance.ledger.ledger_adjustments.track",     "finance.ledger", "track",     "Track Ledger Adjustments."),
    ("finance.ledger.ledger_integrity.validate",    "finance.ledger", "validate",  "Validate Ledger Integrity."),
    ("finance.ledger.approval_controls.enforce",    "finance.ledger", "enforce",   "Enforce Approval Controls."),
    ("finance.ledger.financial_records.audit",      "finance.ledger", "audit",     "Audit Financial Records."),
    ("finance.ledger.financial_reports.schedule",   "finance.ledger", "schedule",  "Schedule Financial Reports."),

    # ==========================================================================
    # TIER 4 — MODULE 19: DISCOUNTS, REFUNDS & ADJUSTMENTS
    # ==========================================================================
    ("finance.adjustment.discount_policy.create",           "finance.adjustment", "create",   "Create Discount Policy."),
    ("finance.adjustment.discounts_students.assign",        "finance.adjustment", "assign",   "Assign Discounts to Students."),
    ("finance.adjustment.bulk_discounts.apply",             "finance.adjustment", "apply",    "Apply Bulk Discounts."),
    ("finance.adjustment.discount_eligibility.validate",    "finance.adjustment", "validate", "Validate Discount Eligibility."),
    ("finance.adjustment.discount_requests.approve",        "finance.adjustment", "approve",  "Approve Discount Requests."),
    ("finance.adjustment.discounts.revoke",                 "finance.adjustment", "revoke",   "Revoke Discounts."),
    ("finance.adjustment.refund_request.initiate",          "finance.adjustment", "initiate", "Initiate Refund Request."),
    ("finance.adjustment.refund_amount.validate",           "finance.adjustment", "validate", "Validate Refund Amount."),
    ("finance.adjustment.refund_workflow.approve",          "finance.adjustment", "approve",  "Approve Refund Workflow."),
    ("finance.adjustment.refund_payment.execute",           "finance.adjustment", "execute",  "Execute Refund Payment."),
    ("finance.adjustment.billing_adjustments.apply",        "finance.adjustment", "apply",    "Apply Billing Adjustments."),
    ("finance.adjustment.financial_entries.reverse",        "finance.adjustment", "reverse",  "Reverse Financial Entries."),
    ("finance.adjustment.adjustment_history.track",         "finance.adjustment", "track",    "Track Adjustment History."),
    ("finance.adjustment.stakeholders_adjustments.notify",  "finance.adjustment", "notify",   "Notify Stakeholders of Adjustments."),
    ("finance.adjustment.adjustment_limits.enforce",        "finance.adjustment", "enforce",  "Enforce Adjustment Limits."),
    ("finance.adjustment.adjustments_post_approval.lock",   "finance.adjustment", "lock",     "Lock Adjustments Post-Approval."),
    ("finance.adjustment.duplicate_refunds.prevent",        "finance.adjustment", "prevent",  "Prevent Duplicate Refunds."),
    ("finance.adjustment.adjustment_reports.export",        "finance.adjustment", "export",   "Export Adjustment Reports."),
    ("finance.adjustment.discount_actions.audit",           "finance.adjustment", "audit",    "Audit Discount Actions."),
    ("finance.adjustment.refund_activities.audit",          "finance.adjustment", "audit",    "Audit Refund Activities."),

    # ==========================================================================
    # TIER 5 — MODULE 20: VENDOR MANAGEMENT
    # ==========================================================================
    ("procurement.vendor.vendor.register",              "procurement.vendor", "register",   "Register Vendor."),
    ("procurement.vendor.vendor_registration.approve",  "procurement.vendor", "approve",    "Approve Vendor Registration."),
    ("procurement.vendor.vendors.categorize",           "procurement.vendor", "categorize", "Categorize Vendors."),
    ("procurement.vendor.vendor_profile.update",        "procurement.vendor", "update",     "Update Vendor Profile."),
    ("procurement.vendor.vendor.deactivate",            "procurement.vendor", "deactivate", "Deactivate Vendor."),
    ("procurement.vendor.vendor_performance.rate",      "procurement.vendor", "rate",       "Rate Vendor Performance."),
    ("procurement.vendor.vendor_history.track",         "procurement.vendor", "track",      "Track Vendor History."),
    ("procurement.vendor.vendor_contracts.assign",      "procurement.vendor", "assign",     "Assign Vendor Contracts."),
    ("procurement.vendor.vendor_documents.upload",      "procurement.vendor", "upload",     "Upload Vendor Documents."),
    ("procurement.vendor.vendor_compliance.validate",   "procurement.vendor", "validate",   "Validate Vendor Compliance."),
    ("procurement.vendor.vendor_directory.search",      "procurement.vendor", "search",     "Search Vendor Directory."),
    ("procurement.vendor.vendor_list.export",           "procurement.vendor", "export",     "Export Vendor List."),
    ("procurement.vendor.duplicate_vendors.detect",     "procurement.vendor", "detect",     "Detect Duplicate Vendors."),
    ("procurement.vendor.vendor_records.lock",          "procurement.vendor", "lock",       "Lock Vendor Records."),
    ("procurement.vendor.vendor_account.restore",       "procurement.vendor", "restore",    "Restore Vendor Account."),
    ("procurement.vendor.vendor_spend_summary.view",    "procurement.vendor", "view",       "View Vendor Spend Summary."),
    ("procurement.vendor.vendor_payment_status.track",  "procurement.vendor", "track",      "Track Vendor Payment Status."),
    ("procurement.vendor.vendor_changes.audit",         "procurement.vendor", "audit",      "Audit Vendor Changes."),

    # ==========================================================================
    # TIER 5 — MODULE 21: PROCUREMENT REQUESTS & APPROVALS
    # ==========================================================================
    ("procurement.request.purchase_request.create",         "procurement.request", "create",   "Create Purchase Request."),
    ("procurement.request.purchase_request.edit",           "procurement.request", "edit",     "Edit Purchase Request."),
    ("procurement.request.budget_availability.validate",    "procurement.request", "validate", "Validate Budget Availability."),
    ("procurement.request.request_approval.submit",         "procurement.request", "submit",   "Submit Request for Approval."),
    ("procurement.request.approval_workflow.route",         "procurement.request", "route",    "Route Approval Workflow."),
    ("procurement.request.purchase_request.approve",        "procurement.request", "approve",  "Approve Purchase Request."),
    ("procurement.request.purchase_request.reject",         "procurement.request", "reject",   "Reject Purchase Request."),
    ("procurement.request.high_value_requests.escalate",    "procurement.request", "escalate", "Escalate High-Value Requests."),
    ("procurement.request.approval_status.track",           "procurement.request", "track",    "Track Approval Status."),
    ("procurement.request.approval_comments.add",           "procurement.request", "add",      "Add Approval Comments."),
    ("procurement.request.spending_limits.enforce",         "procurement.request", "enforce",  "Enforce Spending Limits."),
    ("procurement.request.purchase_request.cancel",         "procurement.request", "cancel",   "Cancel Purchase Request."),
    ("procurement.request.request_stakeholders.notify",     "procurement.request", "notify",   "Notify Request Stakeholders."),
    ("procurement.request.request_po.convert",              "procurement.request", "convert",  "Convert Request to PO."),
    ("procurement.request.request_history.track",           "procurement.request", "track",    "Track Request History."),
    ("procurement.request.request_data.export",             "procurement.request", "export",   "Export Request Data."),
    ("procurement.request.role_based_approvals.enforce",    "procurement.request", "enforce",  "Enforce Role-Based Approvals."),
    ("procurement.request.request_actions.audit",           "procurement.request", "audit",    "Audit Request Actions."),

    # ==========================================================================
    # TIER 5 — MODULE 22: PURCHASE ORDERS & DELIVERY
    # ==========================================================================
    ("procurement.purchase_order.purchase_order.generate",      "procurement.purchase_order", "generate",   "Generate Purchase Order."),
    ("procurement.purchase_order.vendor_po.assign",             "procurement.purchase_order", "assign",     "Assign Vendor to PO."),
    ("procurement.purchase_order.po_vendor.send",               "procurement.purchase_order", "send",       "Send PO to Vendor."),
    ("procurement.purchase_order.po_status.update",             "procurement.purchase_order", "update",     "Update PO Status."),
    ("procurement.purchase_order.po_receipt.acknowledge",       "procurement.purchase_order", "acknowledge","Acknowledge PO Receipt."),
    ("procurement.purchase_order.delivery_timeline.track",      "procurement.purchase_order", "track",      "Track Delivery Timeline."),
    ("procurement.purchase_order.goods_received.record",        "procurement.purchase_order", "record",     "Record Goods Received."),
    ("procurement.purchase_order.delivered_quantity.validate",  "procurement.purchase_order", "validate",   "Validate Delivered Quantity."),
    ("procurement.purchase_order.delivered_quality.validate",   "procurement.purchase_order", "validate",   "Validate Delivered Quality."),
    ("procurement.purchase_order.delivered_items.reject",       "procurement.purchase_order", "reject",     "Reject Delivered Items."),
    ("procurement.purchase_order.delivery_completion.approve",  "procurement.purchase_order", "approve",    "Approve Delivery Completion."),
    ("procurement.purchase_order.invoice_matching.trigger",     "procurement.purchase_order", "trigger",    "Trigger Invoice Matching."),
    ("procurement.purchase_order.purchase_order.close",         "procurement.purchase_order", "close",      "Close Purchase Order."),
    ("procurement.purchase_order.purchase_order.cancel",        "procurement.purchase_order", "cancel",     "Cancel Purchase Order."),
    ("procurement.purchase_order.po_documents.export",          "procurement.purchase_order", "export",     "Export PO Documents."),
    ("procurement.purchase_order.po_history.track",             "procurement.purchase_order", "track",      "Track PO History."),
    ("procurement.purchase_order.delivery_stakeholders.notify", "procurement.purchase_order", "notify",     "Notify Delivery Stakeholders."),
    ("procurement.purchase_order.po_changes.audit",             "procurement.purchase_order", "audit",      "Audit PO Changes."),

    # ==========================================================================
    # TIER 5 — MODULE 23: INVENTORY & ASSET TRACKING
    # ==========================================================================
    ("inventory.inventory_item.register",       "inventory", "register",  "Register Inventory Item."),
    ("inventory.inventory_category.assign",     "inventory", "assign",    "Assign Inventory Category."),
    ("inventory.inventory_quantity.track",      "inventory", "track",     "Track Inventory Quantity."),
    ("inventory.stock_levels.update",           "inventory", "update",    "Update Stock Levels."),
    ("inventory.reorder_thresholds.set",        "inventory", "set",       "Set Reorder Thresholds."),
    ("inventory.stock_alerts.generate",         "inventory", "generate",  "Generate Stock Alerts."),
    ("inventory.asset_acquisition.record",      "inventory", "record",    "Record Asset Acquisition."),
    ("inventory.asset_location.assign",         "inventory", "assign",    "Assign Asset Location."),
    ("inventory.asset_depreciation.track",      "inventory", "track",     "Track Asset Depreciation."),
    ("inventory.asset_disposal.record",         "inventory", "record",    "Record Asset Disposal."),
    ("inventory.inventory_movements.audit",     "inventory", "audit",     "Audit Inventory Movements."),
    ("inventory.stock_reconciliation.perform",  "inventory", "perform",   "Perform Stock Reconciliation."),
    ("inventory.inventory_records.lock",        "inventory", "lock",      "Lock Inventory Records."),
    ("inventory.inventory_reports.export",      "inventory", "export",    "Export Inventory Reports."),
    ("inventory.asset_history.track",           "inventory", "track",     "Track Asset History."),
    ("inventory.assets_departments.assign",     "inventory", "assign",    "Assign Assets to Departments."),
    ("inventory.inventory_permissions.enforce", "inventory", "enforce",   "Enforce Inventory Permissions."),
    ("inventory.asset_records.archive",         "inventory", "archive",   "Archive Asset Records."),

    # ==========================================================================
    # TIER 6 — MODULE 24: DASHBOARDS & KPIs
    # ==========================================================================
    ("analytics.dashboard.school_overview_dashboard.view", "analytics.dashboard", "view",      "Display School Overview Dashboard."),
    ("analytics.dashboard.academic_performance_kpis.view",      "analytics.dashboard", "view",      "Display Academic Performance KPIs."),
    ("analytics.dashboard.attendance_kpis.view",                "analytics.dashboard", "view",      "Display Attendance KPIs."),
    ("analytics.dashboard.financial_kpis.view",                 "analytics.dashboard", "view",      "Display Financial KPIs."),
    ("analytics.dashboard.procurement_kpis.view",               "analytics.dashboard", "view",      "Display Procurement KPIs."),
    ("analytics.dashboard.dashboard_metrics.filter",            "analytics.dashboard", "filter",    "Filter Dashboard Metrics."),
    ("analytics.dashboard.kpis.drilldown",                      "analytics.dashboard", "drilldown", "Drill Down into KPIs."),
    ("analytics.dashboard.period_performance.compare",          "analytics.dashboard", "compare",   "Compare Period Performance."),
    ("analytics.dashboard.dashboard_widgets.configure",         "analytics.dashboard", "configure", "Configure Dashboard Widgets."),
    ("analytics.dashboard.custom_dashboards.save",              "analytics.dashboard", "save",      "Save Custom Dashboards."),
    ("analytics.dashboard.dashboard_access_control.enforce",    "analytics.dashboard", "enforce",   "Enforce Dashboard Access Control."),
    ("analytics.dashboard.dashboard_data.refresh",              "analytics.dashboard", "refresh",   "Refresh Dashboard Data."),
    ("analytics.dashboard.dashboard_views.export",              "analytics.dashboard", "export",    "Export Dashboard Views."),
    ("analytics.dashboard.dashboard_usage.track",               "analytics.dashboard", "track",     "Track Dashboard Usage."),
    ("analytics.dashboard.dashboard_changes.audit",             "analytics.dashboard", "audit",     "Audit Dashboard Changes."),

    # ==========================================================================
    # TIER 6 — MODULE 25: OPERATIONAL REPORTS & EXPORT
    # ==========================================================================
    ("reporting.operational.attendance_reports.generate",   "reporting.operational", "generate", "Generate Attendance Reports."),
    ("reporting.operational.academic_reports.generate",     "reporting.operational", "generate", "Generate Academic Reports."),
    ("reporting.operational.financial_reports.generate",    "reporting.operational", "generate", "Generate Financial Reports."),
    ("reporting.operational.procurement_reports.generate",  "reporting.operational", "generate", "Generate Procurement Reports."),
    ("reporting.operational.user_activity_reports.generate","reporting.operational", "generate", "Generate User Activity Reports."),
    ("reporting.operational.report_generation.schedule",    "reporting.operational", "schedule", "Schedule Report Generation."),
    ("reporting.operational.report_parameters.filter",      "reporting.operational", "filter",   "Filter Report Parameters."),
    ("reporting.operational.reports.preview",               "reporting.operational", "preview",  "Preview Reports."),
    ("reporting.operational.reports_pdf.export",            "reporting.operational", "export",   "Export Reports to PDF."),
    ("reporting.operational.reports_excel.export",          "reporting.operational", "export",   "Export Reports to Excel."),
    ("reporting.operational.reports_email.share",           "reporting.operational", "share",    "Share Reports via Email."),
    ("reporting.operational.generated_reports.archive",     "reporting.operational", "archive",  "Archive Generated Reports."),
    ("reporting.operational.report_history.track",          "reporting.operational", "track",    "Track Report History."),
    ("reporting.operational.report_access_control.enforce", "reporting.operational", "enforce",  "Enforce Report Access Control."),
    ("reporting.operational.report_generation.audit",       "reporting.operational", "audit",    "Audit Report Generation."),

    # ==========================================================================
    # TIER 6 — MODULE 26: STUDENT PORTAL
    # ==========================================================================
    ("student_portal.student_login.create",         "student_portal", "create",   "Create Student Login."),
    ("student_portal.class_timetable.view",         "student_portal", "view",     "View Class Timetable."),
    ("student_portal.attendance_summary.view",      "student_portal", "view",     "View Attendance Summary."),
    ("student_portal.academic_results.view",        "student_portal", "view",     "View Academic Results."),
    ("student_portal.learning_materials.access",    "student_portal", "access",   "Access Learning Materials."),
    ("student_portal.announcements.receive",        "student_portal", "receive",  "Receive Announcements."),
    ("student_portal.fee_status.view",              "student_portal", "view",     "View Fee Status."),
    ("student_portal.receipts.download",            "student_portal", "download", "Download Receipts."),
    ("student_portal.teachers.message",             "student_portal", "message",  "Message Teachers."),
    ("student_portal.profile_details.update",       "student_portal", "update",   "Update Profile Details."),
    ("student_portal.academic_progress.track",      "student_portal", "track",    "Track Academic Progress."),
    ("student_portal.student_permissions.enforce",  "student_portal", "enforce",  "Enforce Student Permissions."),
    ("student_portal.student_activity.audit",       "student_portal", "audit",    "Audit Student Activity."),

    # ==========================================================================
    # TIER 6 — MODULE 27: PARENT/GUARDIAN PORTAL
    # ==========================================================================
    ("parent_portal.parent_login.create",           "parent_portal", "create",   "Parent Self-Service Login."),
    ("parent_portal.child_attendance.view",         "parent_portal", "view",     "View Child Attendance Summary and Absence Alerts."),
    ("parent_portal.child_academic_results.view",   "parent_portal", "view",     "View Child Academic Results and Report Cards."),
    ("parent_portal.fee_status.view",               "parent_portal", "view",     "View Fee Status and Outstanding Invoices."),
    ("parent_portal.fee_payments.make",             "parent_portal", "pay",      "Make Online Fee Payments from Portal."),
    ("parent_portal.payment_receipts.download",     "parent_portal", "download", "Download Payment Receipts."),
    ("parent_portal.announcements.receive",         "parent_portal", "receive",  "Receive School Announcements and Notifications."),
    ("parent_portal.teachers_admin.message",        "parent_portal", "message",  "Message Relevant Teachers or Admin."),
    ("parent_portal.child_timetable.view",          "parent_portal", "view",     "View Child Timetable."),
    ("parent_portal.contact_details.update",        "parent_portal", "update",   "Update Parent Contact Details."),
    ("parent_portal.multiple_children.manage",      "parent_portal", "manage",   "Manage Multiple Children from Single Account."),
    ("parent_portal.portal_activity.audit",         "parent_portal", "audit",    "Audit All Parent Portal Activity."),

    # ==========================================================================
    # TIER 7 — EXPORT & DATA ACCESS (cross-cutting)
    # ==========================================================================
    ("data_export.student_data.export",             "data_export", "export",    "Export Student Data."),
    ("data_export.staff_data.export",               "data_export", "export",    "Export Staff Data."),
    ("data_export.attendance_data.export",          "data_export", "export",    "Export Attendance Data."),
    ("data_export.academic_results.export",         "data_export", "export",    "Export Academic Results."),
    ("data_export.financial_records.export",        "data_export", "export",    "Export Financial Records."),
    ("data_export.procurement_records.export",      "data_export", "export",    "Export Procurement Records."),
    ("data_export.audit_logs.export",               "data_export", "export",    "Export Audit Logs."),
    ("data_export.export_formats.configure",        "data_export", "configure", "Configure Export Formats."),
    ("data_export.data_access_permissions.enforce", "data_export", "enforce",   "Enforce Data Access Permissions."),
    ("data_export.data_masking_rules.apply",        "data_export", "apply",     "Apply Data Masking Rules."),
    ("data_export.automated_exports.schedule",      "data_export", "schedule",  "Schedule Automated Exports."),
    ("data_export.secure_download_links.generate",  "data_export", "generate",  "Generate Secure Download Links."),
    ("data_export.export_requests.track",           "data_export", "track",     "Track Export Requests."),
    ("data_export.export_files.expire",             "data_export", "expire",    "Expire Export Files."),
    ("data_export.data_access_events.audit",        "data_export", "audit",     "Audit Data Access Events."),
]

# ---------------------------------------------------------------------------
# Dependency catalog
# Each entry: (permission_key, depends_on_key, is_hard_dependency)
# is_hard_dependency=True means the dependency MUST be present before assignment
# ---------------------------------------------------------------------------

# Shared base deps applied to almost every business permission
_BASE = [
    "system.authenticated.access",
    "system.api.access",
    "system.tenant_context.require",
    "system.tenant_boundary.enforce",
]

# Export-style extra deps
_EXPORT_EXTRAS = [
    "system.download.secure_link.generate",
    "system.export.expiry.enforce",
    "system.data_masking.apply",
]


def _base(hard=True):
    return [(_dep, hard) for _dep in _BASE]


def _base_audit(hard=True):
    return _base(hard) + [("system.audit.write", hard)]


def _base_export(hard=True):
    return _base(hard) + [(_d, hard) for _d in _EXPORT_EXTRAS]


DEPENDENCIES: list[tuple[str, str, bool]] = [
    # (permission_key, depends_on_key, is_hard)

    # ------------------------------------------------------------------
    # GLOBAL PLATFORM GUARDS
    # ------------------------------------------------------------------
    # system.authenticated.access — root, no deps
    # system.session.access.authenticate — root, no deps
    *[("system.api.access", d, True) for d, _ in _base()[:1]],      # depends on authenticated.access
    *[("system.tenant_context.require", d, True) for d, _ in _base()[:1]],
    *[("system.tenant_boundary.enforce", d, True) for d, _ in _base()[:1]],

    ("system.mfa.enforce",              "system.authenticated.access",  True),
    ("system.mfa.reset",                "system.authenticated.access",  True),
    ("system.mfa.reset",                "system.audit.write",           True),
    ("system.password.policy.enforce",  "system.authenticated.access",  True),
    ("system.token.refresh",            "system.authenticated.access",  True),
    ("system.rate_limit.login.enforce", "system.authenticated.access",  True),

    ("system.session.view",             "system.authenticated.access",  True),
    ("system.session.force_logout",     "system.authenticated.access",  True),
    ("system.session.force_logout",     "system.session.view",          True),
    ("system.session.force_logout",     "system.audit.write",           True),

    ("system.audit.write",              "system.authenticated.access",  True),
    ("system.audit.view",               "system.authenticated.access",  True),
    ("system.audit.search",             "system.authenticated.access",  True),
    ("system.audit.search",             "system.audit.view",            True),
    ("system.audit.export",             "system.authenticated.access",  True),
    ("system.audit.export",             "system.audit.view",            True),
    ("system.audit.export",             "system.download.secure_link.generate", True),
    ("system.audit.export",             "system.export.expiry.enforce", True),
    ("system.audit.export",             "system.data_masking.apply",    True),

    ("system.configuration.view",       "system.authenticated.access",  True),
    ("system.configuration.update",     "system.authenticated.access",  True),
    ("system.configuration.update",     "system.configuration.view",    True),
    ("system.configuration.update",     "system.audit.write",           True),

    ("system.feature_flag.view",        "system.authenticated.access",  True),
    ("system.feature_flag.toggle",      "system.authenticated.access",  True),
    ("system.feature_flag.toggle",      "system.feature_flag.view",     True),
    ("system.feature_flag.toggle",      "system.audit.write",           True),
    ("system.feature_flag.schedule",    "system.authenticated.access",  True),
    ("system.feature_flag.schedule",    "system.feature_flag.view",     True),

    ("system.health.view",              "system.authenticated.access",  True),
    ("system.security_alert.view",      "system.authenticated.access",  True),

    ("system.support.impersonation.impersonate", "system.authenticated.access", True),
    ("system.support.impersonation.impersonate", "system.audit.write",          True),
    ("system.support.impersonation.impersonate", "system.mfa.enforce",          True),

    ("system.download.secure_link.generate", "system.authenticated.access", True),
    ("system.data_masking.apply",       "system.authenticated.access",  True),
    ("system.data_masking.apply",       "system.audit.write",           True),
    ("system.export.schedule",          "system.authenticated.access",  True),
    ("system.export.expiry.enforce",    "system.authenticated.access",  True),

    ("system.role_request.approve",     "system.authenticated.access",  True),
    ("system.role_request.approve",     "system.audit.write",           True),
    ("system.role_request.deny",        "system.authenticated.access",  True),
    ("system.role_request.deny",        "system.audit.write",           True),
    ("system.role_request.deny",        "system.role_request.approve",  True),

    ("system.approval.override.super_admin", "system.authenticated.access", True),
    ("system.approval.override.super_admin", "system.mfa.enforce",          True),

    # ------------------------------------------------------------------
    # MODULE 1: SCHOOL & BRANCH MANAGEMENT
    # ------------------------------------------------------------------
    *[("school.school.create",             d, h) for d, h in _base_audit()],
    *[("school.school_slug.generate",      d, h) for d, h in _base()],
    *[("school.database_schema.provision",      d, h) for d, h in _base_audit()],
    *[("school.localization.assign",            d, h) for d, h in _base_audit()],
    *[("school.lifecycle.configure",            d, h) for d, h in _base_audit()],
    *[("school.module_access.toggle",           d, h) for d, h in _base_audit()],
    *[("school.primary_administrator.assign",   d, h) for d, h in _base_audit()],
    *[("school.branding_assets.upload",         d, h) for d, h in _base_audit()],
    *[("school.metadata.update",                d, h) for d, h in _base_audit()],
    *[("school.uniqueness.validate",            d, h) for d, h in _base()],
    *[("school.data_isolation.enforce",         d, h) for d, h in _base()],
    *[("school.access_state.suspend",           d, h) for d, h in _base_audit()],
    *[("school.access_state.reactivate",        d, h) for d, h in _base_audit()],
    *[("school.deletion_state.soft_delete",     d, h) for d, h in _base_audit()],
    *[("school.deletion_state.hard_delete",     d, h) for d, h in _base_audit()],
    ("school.deletion_state.hard_delete",       "school.deletion_state.soft_delete", True),
    *[("school.configuration.reset",            d, h) for d, h in _base_audit()],
    *[("school.health_status.view",             d, h) for d, h in _base()],
    *[("school.creation_audit_logs.track",      d, h) for d, h in _base()],
    *[("school.feature_flags.enforce",          d, h) for d, h in _base()],
    *[("school.provisioning.rollback",          d, h) for d, h in _base_audit()],

    # ------------------------------------------------------------------
    # MODULE 2: VISION ADMIN CONSOLE
    # ------------------------------------------------------------------
    *[("vision_admin.session.authenticate",                      d, h) for d, h in _base()],
    *[("vision_admin.school_dashboard.view",               d, h) for d, h in _base()],
    *[("vision_admin.school_configuration.edit",           d, h) for d, h in _base_audit()],
    ("vision_admin.school_configuration.edit",              "vision_admin.school_dashboard.view", False),
    *[("vision_admin.access_state.suspend_manage",              d, h) for d, h in _base_audit()],
    *[("vision_admin.data_configuration.reset",                 d, h) for d, h in _base_audit()],
    *[("vision_admin.school_provisioning_pipeline.monitor",d, h) for d, h in _base()],
    *[("vision_admin.failed_provisioning_steps.retry",          d, h) for d, h in _base_audit()],
    ("vision_admin.failed_provisioning_steps.retry",             "vision_admin.school_provisioning_pipeline.monitor", False),
    *[("vision_admin.school_usage_metrics.view",           d, h) for d, h in _base()],
    *[("vision_admin.school_audit_logs.access",            d, h) for d, h in _base()],
    ("vision_admin.school_audit_logs.access",               "system.audit.view", True),
    *[("vision_admin.school_user.impersonate",             d, h) for d, h in _base_audit()],
    ("vision_admin.school_user.impersonate",                "system.mfa.enforce", True),
    ("vision_admin.school_user.impersonate",                "system.support.impersonation.impersonate", True),
    *[("vision_admin.role_change_request.manage",               d, h) for d, h in _base_audit()],
    *[("vision_admin.import_jobs.review",                       d, h) for d, h in _base()],
    *[("vision_admin.data_fix.apply",                           d, h) for d, h in _base_audit()],
    *[("vision_admin.feature_flags.toggle",                     d, h) for d, h in _base_audit()],
    *[("vision_admin.health_metrics.view",                      d, h) for d, h in _base()],
    *[("vision_admin.admin_roles.manage",                       d, h) for d, h in _base_audit()],
    *[("vision_admin.mfa.enforce",                              d, h) for d, h in _base()],
    *[("vision_admin.security_alerts.view",                     d, h) for d, h in _base()],
    ("vision_admin.security_alerts.view",                        "system.security_alert.view", False),
    *[("vision_admin.admin_actions.log",                        d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 3: USER IDENTITY, ACCOUNTS & AUTHENTICATION
    # ------------------------------------------------------------------
    *[("identity.user_account.create",              d, h) for d, h in _base_audit()],
    *[("identity.user_email.invite",                d, h) for d, h in _base_audit()],
    ("identity.user_email.invite",                   "identity.user_account.create", False),
    *[("identity.user_account.activate",            d, h) for d, h in _base_audit()],
    ("identity.user_account.activate",               "identity.user_account.create", True),
    *[("identity.password_policy.enforce",          d, h) for d, h in _base()],
    *[("identity.access_token.refresh",             d, h) for d, h in _base()],
    *[("identity.user_account.lock",                d, h) for d, h in _base_audit()],
    ("identity.user_account.lock",                   "identity.user_account.create", True),
    *[("identity.user_account.unlock",              d, h) for d, h in _base_audit()],
    ("identity.user_account.unlock",                 "identity.user_account.create", True),
    ("identity.user_account.unlock",                 "identity.user_account.lock",   False),
    *[("identity.login_sessions.track",             d, h) for d, h in _base()],
    *[("identity.user_logout.force",                d, h) for d, h in _base_audit()],
    ("identity.user_logout.force",                   "identity.login_sessions.track", False),
    *[("identity.user_password.reset",              d, h) for d, h in _base_audit()],
    *[("identity.email_address.verify",             d, h) for d, h in _base()],
    *[("identity.phone_number.verify",              d, h) for d, h in _base()],
    *[("identity.school_aware_login.enforce",  d, h) for d, h in _base()],
    *[("identity.authentication_events.log",        d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 4: ROLES, PERMISSIONS & ACCESS CONTROL
    # ------------------------------------------------------------------
    *[("rbac.role_template.create",               d, h) for d, h in _base_audit()],
    *[("rbac.role_user.assign",                   d, h) for d, h in _base_audit()],
    ("rbac.role_user.assign",                      "rbac.role_template.create",  False),
    *[("rbac.role_permissions.update",            d, h) for d, h in _base_audit()],
    ("rbac.role_permissions.update",               "rbac.role_template.create",  False),
    *[("rbac.role_based_access_control.enforce",  d, h) for d, h in _base()],
    *[("rbac.cross_module_access.restrict",       d, h) for d, h in _base()],
    *[("rbac.permission_dependencies.validate",   d, h) for d, h in _base()],
    *[("rbac.permission_changes_instantly.apply", d, h) for d, h in _base_audit()],
    *[("rbac.role_change.request",                d, h) for d, h in _base()],
    *[("rbac.role_change.approve",                d, h) for d, h in _base_audit()],
    ("rbac.role_change.approve",                   "rbac.role_change.request",   True),
    ("rbac.role_change.approve",                   "system.role_request.approve",True),
    *[("rbac.role_change_request.deny",           d, h) for d, h in _base_audit()],
    ("rbac.role_change_request.deny",              "rbac.role_change.request",   True),
    ("rbac.role_change_request.deny",              "system.role_request.approve",True),
    *[("rbac.multiple_roles_user.assign",         d, h) for d, h in _base_audit()],
    ("rbac.multiple_roles_user.assign",             "rbac.role_user.assign",      True),
    *[("rbac.role.deactivate",                    d, h) for d, h in _base_audit()],
    ("rbac.role.deactivate",                       "rbac.role_template.create",   False),
    *[("rbac.role_template.archive",              d, h) for d, h in _base_audit()],
    ("rbac.role_template.archive",                 "rbac.role_template.create",   True),
    *[("rbac.api_level_permission_checks.enforce",d, h) for d, h in _base()],
    *[("rbac.role_changes.audit",                 d, h) for d, h in _base()],
    *[("rbac.compliance_constraints.enforce",     d, h) for d, h in _base()],
    *[("rbac.permission_matrix.view",             d, h) for d, h in _base()],
    *[("rbac.previous_role_state.restore",        d, h) for d, h in _base_audit()],
    *[("rbac.critical_system_roles.lock",         d, h) for d, h in _base_audit()],

    # ------------------------------------------------------------------
    # MODULE 5: AUDIT LOGGING & COMPLIANCE
    # ------------------------------------------------------------------
    *[("audit.user_creation_events.log",     d, h) for d, h in _base()],
    *[("audit.authentication_events.log",    d, h) for d, h in _base()],
    *[("audit.data_import_actions.log",      d, h) for d, h in _base()],
    *[("audit.configuration_changes.log",    d, h) for d, h in _base()],
    *[("audit.financial_transactions.log",   d, h) for d, h in _base()],
    *[("audit.procurement_actions.log",      d, h) for d, h in _base()],
    *[("audit.permission_changes.log",       d, h) for d, h in _base()],
    *[("audit.immutable_log_storage.enforce",d, h) for d, h in _base()],
    *[("audit.audit_events.timestamp",       d, h) for d, h in _base()],
    *[("audit.actions_actor.attribute",      d, h) for d, h in _base()],
    *[("audit.audit_logs.search",            d, h) for d, h in _base()],
    ("audit.audit_logs.search",               "system.audit.view",  False),
    *[("audit.logs_by_action_type.filter",   d, h) for d, h in _base()],
    ("audit.logs_by_action_type.filter",      "system.audit.view",  False),
    *[("audit.logs_by_date_range.filter",    d, h) for d, h in _base()],
    ("audit.logs_by_date_range.filter",       "system.audit.view",  False),
    *[("audit.audit_logs.export",            d, h) for d, h in _base_export()],
    ("audit.audit_logs.export",               "system.audit.view",  True),
    *[("audit.log_retrieval.paginate",       d, h) for d, h in _base()],
    *[("audit.audit_trail_entity.view",      d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 6: SYSTEM CONFIGURATION & FEATURE FLAGS
    # ------------------------------------------------------------------
    *[("system_config.global_system_settings.view",                   d, h) for d, h in _base()],
    *[("system_config.global_system_settings.update",                 d, h) for d, h in _base_audit()],
    ("system_config.global_system_settings.update",                    "system_config.global_system_settings.view", True),
    *[("system_config.configuration_key.create",                      d, h) for d, h in _base_audit()],
    *[("system_config.configuration_key.edit",                        d, h) for d, h in _base_audit()],
    ("system_config.configuration_key.edit",                           "system_config.configuration_key.create", False),
    *[("system_config.configuration_key.delete",                      d, h) for d, h in _base_audit()],
    ("system_config.configuration_key.delete",                         "system_config.configuration_key.create", True),
    *[("system_config.deleted_configuration_key.restore",             d, h) for d, h in _base_audit()],
    *[("system_config.configuration_schema.validate",                 d, h) for d, h in _base()],
    *[("system_config.configuration_access_permissions.enforce",      d, h) for d, h in _base()],
    *[("system_config.configuration_change_history.track",            d, h) for d, h in _base()],
    *[("system_config.configuration_changes.rollback",                d, h) for d, h in _base_audit()],
    ("system_config.configuration_changes.rollback",                   "system_config.configuration_change_history.track", False),
    *[("system_config.feature_flag.create",                           d, h) for d, h in _base_audit()],
    *[("system_config.feature_flag_globally.enable",                  d, h) for d, h in _base_audit()],
    ("system_config.feature_flag_globally.enable",                     "system_config.feature_flag.create", True),
    *[("system_config.feature_flag_globally.disable",                 d, h) for d, h in _base_audit()],
    ("system_config.feature_flag_globally.disable",                    "system_config.feature_flag.create", True),
    *[("system_config.feature_flag_school.enable",               d, h) for d, h in _base_audit()],
    ("system_config.feature_flag_school.enable",                  "system_config.feature_flag.create", True),
    *[("system_config.feature_flag_school.disable",              d, h) for d, h in _base_audit()],
    ("system_config.feature_flag_school.disable",                 "system_config.feature_flag.create", True),
    *[("system_config.feature_flag_activation.schedule",              d, h) for d, h in _base()],
    *[("system_config.feature_flag_deactivation.schedule",            d, h) for d, h in _base()],
    *[("system_config.flags_specific_roles.restrict",                 d, h) for d, h in _base()],
    *[("system_config.feature_flag_changes.audit",                    d, h) for d, h in _base()],
    *[("system_config.conflicting_flag_rules.detect",                 d, h) for d, h in _base()],
    *[("system_config.configuration_and_feature_flag_settings.export",d, h) for d, h in _base_export()],

    # ------------------------------------------------------------------
    # MODULE 7: NOTIFICATION & MESSAGING ENGINE
    # ------------------------------------------------------------------
    *[("communication.internal_message.send",          d, h) for d, h in _base()],
    *[("communication.bulk_notifications.send",         d, h) for d, h in _base()],
    ("communication.bulk_notifications.send",            "communication.internal_message.send", False),
    *[("communication.email_notifications.send",        d, h) for d, h in _base()],
    *[("communication.sms_notifications.send",          d, h) for d, h in _base()],
    *[("communication.notification_templates.configure",d, h) for d, h in _base_audit()],
    *[("communication.files_messages.attach",           d, h) for d, h in _base_audit()],
    ("communication.files_messages.attach",              "communication.internal_message.send", True),
    *[("communication.message_delivery.track",          d, h) for d, h in _base()],
    *[("communication.message_history.view",            d, h) for d, h in _base()],
    *[("communication.messages_by_type.filter",         d, h) for d, h in _base()],
    ("communication.messages_by_type.filter",            "communication.message_history.view", False),
    *[("communication.messages.reply",                  d, h) for d, h in _base()],
    ("communication.messages.reply",                     "communication.internal_message.send", True),
    *[("communication.notification_threads.mute",       d, h) for d, h in _base()],
    *[("communication.notifications.schedule",          d, h) for d, h in _base()],
    *[("communication.communication_permissions.enforce",d, h) for d, h in _base()],
    *[("communication.communication_events.log",        d, h) for d, h in _base()],
    *[("communication.message_activity.audit",          d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 8: SCHOOL ONBOARDING
    # ------------------------------------------------------------------
    *[("onboarding.school_onboarding.initiate",      d, h) for d, h in _base()],
    *[("onboarding.school_metadata.capture",          d, h) for d, h in _base_audit()],
    ("onboarding.school_metadata.capture",             "onboarding.school_onboarding.initiate", True),
    *[("onboarding.academic_structure.configure",          d, h) for d, h in _base_audit()],
    *[("onboarding.financial_structure.configure",         d, h) for d, h in _base_audit()],
    *[("onboarding.procurement_structure.configure",       d, h) for d, h in _base_audit()],
    *[("onboarding.branch_structure.configure",            d, h) for d, h in _base_audit()],
    *[("onboarding.default_roles.assign",                  d, h) for d, h in _base_audit()],
    *[("onboarding.default_permissions.seed",              d, h) for d, h in _base_audit()],
    ("onboarding.default_permissions.seed",                 "onboarding.default_roles.assign", True),
    *[("onboarding.initial_datasets.upload",               d, h) for d, h in _base_audit()],
    *[("onboarding.onboarding_completeness.validate",      d, h) for d, h in _base()],
    *[("onboarding.onboarding_progress.track",             d, h) for d, h in _base()],
    *[("onboarding.go_live_on_validation_errors.block",    d, h) for d, h in _base()],
    ("onboarding.go_live_on_validation_errors.block",       "onboarding.onboarding_completeness.validate", True),
    *[("onboarding.training_session.schedule",             d, h) for d, h in _base()],
    *[("onboarding.onboarding_checklist.generate",         d, h) for d, h in _base()],
    *[("onboarding.onboarding_actions.log",                d, h) for d, h in _base()],
    *[("onboarding.incomplete_onboarding.rollback",        d, h) for d, h in _base_audit()],
    *[("onboarding.post_go_live_activity.monitor",         d, h) for d, h in _base()],
    *[("onboarding.onboarding_issues.escalate",            d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 9: DATA IMPORT & VALIDATION ENGINE
    # ------------------------------------------------------------------
    *[("data_import.csv_file.upload",                 d, h) for d, h in _base_audit()],
    *[("data_import.excel_file.upload",               d, h) for d, h in _base_audit()],
    *[("data_import.dataset_type.detect",             d, h) for d, h in _base()],
    *[("data_import.file_structure.validate",         d, h) for d, h in _base()],
    *[("data_import.mandatory_fields.validate",       d, h) for d, h in _base()],
    *[("data_import.duplicate_records.detect",        d, h) for d, h in _base()],
    *[("data_import.cross_entity_references.validate",d, h) for d, h in _base()],
    *[("data_import.auto_mapping_rules.apply",        d, h) for d, h in _base_audit()],
    *[("data_import.field_mapping.map",               d, h) for d, h in _base_audit()],
    *[("data_import.validation_error_report.generate",d, h) for d, h in _base()],
    *[("data_import.errors_vs_warnings.classify",     d, h) for d, h in _base()],
    *[("data_import.import_on_critical_errors.block", d, h) for d, h in _base()],
    ("data_import.import_on_critical_errors.block",    "data_import.errors_vs_warnings.classify", True),
    *[("data_import.background_import_job.execute",   d, h) for d, h in _base_audit()],
    ("data_import.background_import_job.execute",      "data_import.import_on_critical_errors.block", True),
    *[("data_import.import_progress.track",           d, h) for d, h in _base()],
    *[("data_import.error_import_rows.edit",          d, h) for d, h in _base_audit()],
    *[("data_import.failed_imports.rollback",         d, h) for d, h in _base_audit()],
    *[("data_import.data_changes.log",                d, h) for d, h in _base()],
    *[("data_import.large_dataset_imports.support",   d, h) for d, h in _base()],
    *[("data_import.import_history.store",            d, h) for d, h in _base()],
    *[("data_import.admin_on_completion.notify",      d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 10: STUDENT MANAGEMENT
    # ------------------------------------------------------------------
    *[("student.student_profile.create",               d, h) for d, h in _base_audit()],
    *[("student.student_records.update",               d, h) for d, h in _base_audit()],
    ("student.student_records.update",                  "student.student_profile.create", True),
    *[("student.student_class.assign",                 d, h) for d, h in _base_audit()],
    ("student.student_class.assign",                    "student.student_profile.create", True),
    *[("student.student_between_terms_sessions.promote",d, h) for d, h in _base_audit()],
    ("student.student_between_terms_sessions.promote",  "student.student_class.assign", True),
    *[("student.student_records.archive",              d, h) for d, h in _base_audit()],
    ("student.student_records.archive",                 "student.student_profile.create", True),
    *[("student.archived_students.restore",            d, h) for d, h in _base_audit()],
    ("student.archived_students.restore",               "student.student_records.archive", True),
    *[("student.student_status.manage",                d, h) for d, h in _base_audit()],
    ("student.student_status.manage",                   "student.student_profile.create", True),
    *[("student.student_documents.upload",             d, h) for d, h in _base_audit()],
    ("student.student_documents.upload",                "student.student_profile.create", True),
    *[("student.student_identity.validate",            d, h) for d, h in _base()],
    *[("student.enrollment_history.track",             d, h) for d, h in _base()],
    ("student.enrollment_history.track",                "student.student_profile.create", False),
    *[("student.parent_guardian_information.add",      d, h) for d, h in _base_audit()],
    ("student.parent_guardian_information.add",         "student.student_profile.create", True),
    *[("student.data_privacy_rules.enforce",           d, h) for d, h in _base()],
    *[("student.student_records.search",               d, h) for d, h in _base()],
    *[("student.student_data.export",                  d, h) for d, h in _base_export()],
    ("student.student_data.export",                     "student.student_records.search", False),
    *[("student.student_record_changes.log",           d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 11: STAFF MANAGEMENT
    # ------------------------------------------------------------------
    *[("staff.staff_profile.create",     d, h) for d, h in _base_audit()],
    *[("staff.staff_role.assign",        d, h) for d, h in _base_audit()],
    ("staff.staff_role.assign",           "staff.staff_profile.create", True),
    *[("staff.staff_department.assign",  d, h) for d, h in _base_audit()],
    ("staff.staff_department.assign",     "staff.staff_profile.create", True),
    *[("staff.staff_account.activate",   d, h) for d, h in _base_audit()],
    ("staff.staff_account.activate",      "staff.staff_profile.create", True),
    *[("staff.staff_account.deactivate", d, h) for d, h in _base_audit()],
    ("staff.staff_account.deactivate",    "staff.staff_profile.create", True),
    *[("staff.staff_department.transfer",d, h) for d, h in _base_audit()],
    ("staff.staff_department.transfer",   "staff.staff_department.assign", True),
    *[("staff.staff_activity.track",     d, h) for d, h in _base()],
    *[("staff.staff_permissions.manage", d, h) for d, h in _base_audit()],
    *[("staff.staff_documents.upload",   d, h) for d, h in _base_audit()],
    ("staff.staff_documents.upload",      "staff.staff_profile.create", True),
    *[("staff.staff_records.search",     d, h) for d, h in _base()],
    *[("staff.staff_data.export",        d, h) for d, h in _base_export()],
    ("staff.staff_data.export",           "staff.staff_records.search", False),
    *[("staff.staff_actions.log",        d, h) for d, h in _base()],
    *[("staff.role_constraints.enforce", d, h) for d, h in _base()],
    *[("staff.staff_account.restore",    d, h) for d, h in _base_audit()],
    ("staff.staff_account.restore",       "staff.staff_account.deactivate", True),
    *[("staff.staff_record.archive",     d, h) for d, h in _base_audit()],
    ("staff.staff_record.archive",        "staff.staff_profile.create", True),

    # ------------------------------------------------------------------
    # MODULE 12: ACADEMIC STRUCTURE
    # ------------------------------------------------------------------
    *[("academic_structure.academic_program.create",      d, h) for d, h in _base_audit()],
    *[("academic_structure.department.create",            d, h) for d, h in _base_audit()],
    ("academic_structure.department.create",               "academic_structure.academic_program.create", False),
    *[("academic_structure.class.create",                 d, h) for d, h in _base_audit()],
    ("academic_structure.class.create",                    "academic_structure.academic_program.create", False),
    *[("academic_structure.class_program.assign",         d, h) for d, h in _base_audit()],
    ("academic_structure.class_program.assign",            "academic_structure.class.create", True),
    ("academic_structure.class_program.assign",            "academic_structure.academic_program.create", True),
    *[("academic_structure.teacher_class.assign",         d, h) for d, h in _base_audit()],
    ("academic_structure.teacher_class.assign",            "academic_structure.class.create", True),
    *[("academic_structure.term_structure.define",        d, h) for d, h in _base_audit()],
    *[("academic_structure.academic_calendar.update",     d, h) for d, h in _base_audit()],
    *[("academic_structure.academic_structure.archive",   d, h) for d, h in _base_audit()],
    *[("academic_structure.academic_structures.clone",    d, h) for d, h in _base_audit()],
    *[("academic_structure.structure_dependencies.validate",d,h) for d, h in _base()],
    *[("academic_structure.academic_configuration.lock",  d, h) for d, h in _base_audit()],
    *[("academic_structure.academic_year.rollover",       d, h) for d, h in _base_audit()],
    *[("academic_structure.structural_changes.track",     d, h) for d, h in _base()],
    *[("academic_structure.academic_setup.export",        d, h) for d, h in _base_export()],
    *[("academic_structure.academic_modifications.audit", d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 13: ACADEMIC CALENDAR & TIMETABLES
    # ------------------------------------------------------------------
    *[("academic_calendar.academic_session.create",       d, h) for d, h in _base_audit()],
    *[("academic_calendar.academic_terms.define",         d, h) for d, h in _base_audit()],
    ("academic_calendar.academic_terms.define",            "academic_calendar.academic_session.create", True),
    *[("academic_calendar.school_calendar.configure",     d, h) for d, h in _base_audit()],
    *[("academic_calendar.holidays_and_breaks.set",       d, h) for d, h in _base_audit()],
    *[("academic_calendar.class_timetable.create",        d, h) for d, h in _base_audit()],
    ("academic_calendar.class_timetable.create",           "academic_calendar.academic_session.create", True),
    *[("academic_calendar.subjects_time_slots.assign",    d, h) for d, h in _base_audit()],
    ("academic_calendar.subjects_time_slots.assign",       "academic_calendar.class_timetable.create", True),
    *[("academic_calendar.teachers_timetable.assign",     d, h) for d, h in _base_audit()],
    ("academic_calendar.teachers_timetable.assign",        "academic_calendar.class_timetable.create", True),
    *[("academic_calendar.scheduling_conflicts.prevent",  d, h) for d, h in _base()],
    *[("academic_calendar.timetable.publish",             d, h) for d, h in _base_audit()],
    ("academic_calendar.timetable.publish",                "academic_calendar.timetable.update", True),
    *[("academic_calendar.timetable.update",              d, h) for d, h in _base_audit()],
    ("academic_calendar.timetable.update",                 "academic_calendar.class_timetable.create", True),
    *[("academic_calendar.users_schedule_changes.notify", d, h) for d, h in _base()],
    *[("academic_calendar.academic_calendar.lock",        d, h) for d, h in _base_audit()],
    *[("academic_calendar.academic_calendar.clone",       d, h) for d, h in _base_audit()],
    *[("academic_calendar.calendar_new_year.rollover",    d, h) for d, h in _base_audit()],
    *[("academic_calendar.daily_schedule.view",           d, h) for d, h in _base()],
    *[("academic_calendar.calendar_attendance.sync",      d, h) for d, h in _base_audit()],
    *[("academic_calendar.timetable.export",              d, h) for d, h in _base_export()],
    *[("academic_calendar.calendar_changes.track",        d, h) for d, h in _base()],
    *[("academic_calendar.role_based_editing.enforce",    d, h) for d, h in _base()],
    *[("academic_calendar.calendar_updates.audit",        d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 14: ATTENDANCE MANAGEMENT
    # ------------------------------------------------------------------
    *[("attendance.attendance_rules.configure",           d, h) for d, h in _base_audit()],
    *[("attendance.attendance_types.define",              d, h) for d, h in _base()],
    *[("attendance.class_attendance_roster.load",         d, h) for d, h in _base()],
    *[("attendance.student_attendance.record",            d, h) for d, h in _base_audit()],
    ("attendance.student_attendance.record",               "attendance.class_attendance_roster.load", True),
    *[("attendance.attendance_records.edit",              d, h) for d, h in _base_audit()],
    ("attendance.attendance_records.edit",                 "attendance.student_attendance.record", True),
    *[("attendance.duplicate_attendance_entries.prevent", d, h) for d, h in _base()],
    *[("attendance.unrecorded_attendance.auto_mark",      d, h) for d, h in _base()],
    *[("attendance.attendance_student_profile.sync",      d, h) for d, h in _base_audit()],
    *[("attendance.parents_absence.notify",               d, h) for d, h in _base()],
    ("attendance.parents_absence.notify",                  "communication.sms_notifications.send", False),
    *[("attendance.attendance_summary.generate",          d, h) for d, h in _base()],
    *[("attendance.attendance_anomalies.detect",          d, h) for d, h in _base()],
    *[("attendance.attendance_records.lock",              d, h) for d, h in _base_audit()],
    ("attendance.attendance_records.lock",                 "attendance.student_attendance.record", True),
    *[("attendance.locked_attendance.reopen",             d, h) for d, h in _base_audit()],
    ("attendance.locked_attendance.reopen",                "attendance.attendance_records.lock", True),
    *[("attendance.attendance_history.track",             d, h) for d, h in _base()],
    *[("attendance.attendance_reports.export",            d, h) for d, h in _base_export()],
    *[("attendance.role_based_attendance_access.enforce", d, h) for d, h in _base()],
    *[("attendance.attendance_changes.audit",             d, h) for d, h in _base()],
    *[("attendance.bulk_attendance_updates.support",      d, h) for d, h in _base()],
    ("attendance.bulk_attendance_updates.support",         "attendance.student_attendance.record", True),

    # ------------------------------------------------------------------
    # MODULE 15: GRADEBOOK & ASSESSMENTS
    # ------------------------------------------------------------------
    *[("gradebook.assessment_type.create",        d, h) for d, h in _base_audit()],
    *[("gradebook.grading_scheme.configure",      d, h) for d, h in _base_audit()],
    *[("gradebook.assessments_classes.assign",    d, h) for d, h in _base_audit()],
    ("gradebook.assessments_classes.assign",       "gradebook.assessment_type.create", True),
    *[("gradebook.student_scores.enter",          d, h) for d, h in _base_audit()],
    ("gradebook.student_scores.enter",             "gradebook.assessments_classes.assign", True),
    *[("gradebook.submitted_scores.edit",         d, h) for d, h in _base_audit()],
    ("gradebook.submitted_scores.edit",            "gradebook.student_scores.enter", True),
    *[("gradebook.totals.calculate",              d, h) for d, h in _base()],
    ("gradebook.totals.calculate",                 "gradebook.student_scores.enter", True),
    *[("gradebook.weighted_grading_rules.apply",  d, h) for d, h in _base_audit()],
    ("gradebook.weighted_grading_rules.apply",     "gradebook.grading_scheme.configure", True),
    *[("gradebook.score_ranges.validate",         d, h) for d, h in _base()],
    ("gradebook.score_ranges.validate",            "gradebook.grading_scheme.configure", True),
    *[("gradebook.gradebook.lock",                d, h) for d, h in _base_audit()],
    *[("gradebook.gradebook.unlock",              d, h) for d, h in _base_audit()],
    ("gradebook.gradebook.unlock",                 "gradebook.gradebook.lock", True),
    *[("gradebook.report_cards.generate",         d, h) for d, h in _base()],
    ("gradebook.report_cards.generate",            "gradebook.totals.calculate", True),
    *[("gradebook.term_results.approve",          d, h) for d, h in _base_audit()],
    ("gradebook.term_results.approve",             "gradebook.report_cards.generate", True),
    ("gradebook.term_results.approve",             "system.role_request.approve", True),
    *[("gradebook.results_students.publish",      d, h) for d, h in _base_audit()],
    ("gradebook.results_students.publish",         "gradebook.term_results.approve", True),
    *[("gradebook.parents_results.notify",        d, h) for d, h in _base()],
    ("gradebook.parents_results.notify",           "gradebook.results_students.publish", False),
    *[("gradebook.grade_history.track",           d, h) for d, h in _base()],
    *[("gradebook.missing_grades.detect",         d, h) for d, h in _base()],
    *[("gradebook.grade_reports.export",          d, h) for d, h in _base_export()],
    *[("gradebook.continuous_assessment.support", d, h) for d, h in _base()],
    *[("gradebook.exam_assessments.support",      d, h) for d, h in _base()],
    *[("gradebook.grade_modifications.audit",     d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 16: BILLING & FEES MANAGEMENT
    # ------------------------------------------------------------------
    *[("finance.billing.fee_item.create",             d, h) for d, h in _base_audit()],
    *[("finance.billing.fee_structure.configure",     d, h) for d, h in _base_audit()],
    ("finance.billing.fee_structure.configure",        "finance.billing.fee_item.create", True),
    *[("finance.billing.fees_classes.assign",         d, h) for d, h in _base_audit()],
    ("finance.billing.fees_classes.assign",            "finance.billing.fee_structure.configure", True),
    *[("finance.billing.fees_students.assign",        d, h) for d, h in _base_audit()],
    ("finance.billing.fees_students.assign",           "finance.billing.fee_item.create", True),
    *[("finance.billing.student_invoices.generate",   d, h) for d, h in _base_audit()],
    ("finance.billing.student_invoices.generate",      "finance.billing.fee_structure.configure", True),
    *[("finance.billing.bulk_invoices.generate",      d, h) for d, h in _base_audit()],
    ("finance.billing.bulk_invoices.generate",         "finance.billing.student_invoices.generate", True),
    *[("finance.billing.invoice_items.edit",          d, h) for d, h in _base_audit()],
    ("finance.billing.invoice_items.edit",             "finance.billing.student_invoices.generate", True),
    *[("finance.billing.late_fee_rules.apply",        d, h) for d, h in _base_audit()],
    *[("finance.billing.discounts.apply",             d, h) for d, h in _base_audit()],
    ("finance.billing.discounts.apply",                "finance.billing.student_invoices.generate", True),
    *[("finance.billing.scholarships.apply",          d, h) for d, h in _base_audit()],
    *[("finance.billing.payment_deadlines.configure", d, h) for d, h in _base_audit()],
    *[("finance.billing.invoice_status.track",        d, h) for d, h in _base()],
    *[("finance.billing.issued_invoices.lock",        d, h) for d, h in _base_audit()],
    ("finance.billing.issued_invoices.lock",           "finance.billing.student_invoices.generate", True),
    *[("finance.billing.invoices.cancel",             d, h) for d, h in _base_audit()],
    ("finance.billing.invoices.cancel",                "finance.billing.issued_invoices.lock", False),
    *[("finance.billing.invoices.reissue",            d, h) for d, h in _base_audit()],
    ("finance.billing.invoices.reissue",               "finance.billing.invoices.cancel", False),
    *[("finance.billing.payers_invoices.notify",      d, h) for d, h in _base()],
    *[("finance.billing.fee_history.track",           d, h) for d, h in _base()],
    *[("finance.billing.billing_reports.export",      d, h) for d, h in _base_export()],
    *[("finance.billing.billing_permissions.enforce", d, h) for d, h in _base()],
    *[("finance.billing.billing_actions.audit",       d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 17: PAYMENTS & RECONCILIATION
    # ------------------------------------------------------------------
    *[("finance.payment.payment_gateways.configure",      d, h) for d, h in _base_audit()],
    *[("finance.payment.payment_channels.enable",         d, h) for d, h in _base_audit()],
    ("finance.payment.payment_channels.enable",            "finance.payment.payment_gateways.configure", True),
    *[("finance.payment.online_payments.process",         d, h) for d, h in _base_audit()],
    ("finance.payment.online_payments.process",            "finance.payment.payment_channels.enable", True),
    *[("finance.payment.offline_payments.record",         d, h) for d, h in _base_audit()],
    *[("finance.payment.payment_receipts.generate",       d, h) for d, h in _base()],
    *[("finance.payment.payments_invoices.match",         d, h) for d, h in _base_audit()],
    *[("finance.payment.partial_payments.handle",         d, h) for d, h in _base_audit()],
    ("finance.payment.partial_payments.handle",            "finance.payment.payments_invoices.match", True),
    *[("finance.payment.overpayments.handle",             d, h) for d, h in _base_audit()],
    ("finance.payment.overpayments.handle",                "finance.payment.payments_invoices.match", True),
    *[("finance.payment.failed_payments.detect",          d, h) for d, h in _base()],
    *[("finance.payment.failed_payments.retry",           d, h) for d, h in _base_audit()],
    ("finance.payment.failed_payments.retry",              "finance.payment.failed_payments.detect", True),
    *[("finance.payment.gateway_transactions.reconcile",  d, h) for d, h in _base_audit()],
    *[("finance.payment.bank_transfers.reconcile",        d, h) for d, h in _base_audit()],
    *[("finance.payment.refund_requests.process",         d, h) for d, h in _base_audit()],
    *[("finance.payment.refunds.approve",                 d, h) for d, h in _base_audit()],
    ("finance.payment.refunds.approve",                    "finance.payment.refund_requests.process", True),
    ("finance.payment.refunds.approve",                    "system.role_request.approve",              True),
    *[("finance.payment.refunds.execute",                 d, h) for d, h in _base_audit()],
    ("finance.payment.refunds.execute",                    "finance.payment.refunds.approve",          True),
    *[("finance.payment.payment_status.track",            d, h) for d, h in _base()],
    *[("finance.payment.users_payment_events.notify",     d, h) for d, h in _base()],
    *[("finance.payment.payment_history.export",          d, h) for d, h in _base_export()],
    *[("finance.payment.payment_approvals.enforce",       d, h) for d, h in _base()],
    *[("finance.payment.payment_transactions.audit",      d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 18: FINANCE LEDGER & REPORTING
    # ------------------------------------------------------------------
    *[("finance.ledger.chart_accounts.configure",     d, h) for d, h in _base_audit()],
    *[("finance.ledger.ledger_accounts.create",       d, h) for d, h in _base_audit()],
    ("finance.ledger.ledger_accounts.create",          "finance.ledger.chart_accounts.configure", True),
    *[("finance.ledger.financial_transactions.record",d, h) for d, h in _base_audit()],
    *[("finance.ledger.billing_entries.post",         d, h) for d, h in _base()],
    *[("finance.ledger.payment_entries.post",         d, h) for d, h in _base()],
    *[("finance.ledger.expense_entries.record",       d, h) for d, h in _base_audit()],
    *[("finance.ledger.trial_balance.generate",       d, h) for d, h in _base()],
    *[("finance.ledger.income_statement.generate",    d, h) for d, h in _base()],
    *[("finance.ledger.balance_sheet.generate",       d, h) for d, h in _base()],
    *[("finance.ledger.cash_flow_report.generate",    d, h) for d, h in _base()],
    *[("finance.ledger.outstanding_balances.track",   d, h) for d, h in _base()],
    *[("finance.ledger.financial_reports.filter",     d, h) for d, h in _base()],
    *[("finance.ledger.financial_statements.export",  d, h) for d, h in _base_export()],
    *[("finance.ledger.financial_periods.lock",       d, h) for d, h in _base_audit()],
    *[("finance.ledger.financial_periods.reopen",     d, h) for d, h in _base_audit()],
    ("finance.ledger.financial_periods.reopen",        "finance.ledger.financial_periods.lock", True),
    *[("finance.ledger.ledger_adjustments.track",     d, h) for d, h in _base()],
    *[("finance.ledger.ledger_integrity.validate",    d, h) for d, h in _base()],
    *[("finance.ledger.approval_controls.enforce",    d, h) for d, h in _base()],
    *[("finance.ledger.financial_records.audit",      d, h) for d, h in _base()],
    *[("finance.ledger.financial_reports.schedule",   d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 19: DISCOUNTS, REFUNDS & ADJUSTMENTS
    # ------------------------------------------------------------------
    *[("finance.adjustment.discount_policy.create",           d, h) for d, h in _base_audit()],
    *[("finance.adjustment.discounts_students.assign",        d, h) for d, h in _base_audit()],
    ("finance.adjustment.discounts_students.assign",           "finance.adjustment.discount_policy.create", True),
    *[("finance.adjustment.bulk_discounts.apply",             d, h) for d, h in _base_audit()],
    ("finance.adjustment.bulk_discounts.apply",                "finance.adjustment.discount_policy.create", True),
    *[("finance.adjustment.discount_eligibility.validate",    d, h) for d, h in _base()],
    *[("finance.adjustment.discount_requests.approve",        d, h) for d, h in _base_audit()],
    ("finance.adjustment.discount_requests.approve",           "system.role_request.approve", True),
    *[("finance.adjustment.discounts.revoke",                 d, h) for d, h in _base_audit()],
    ("finance.adjustment.discounts.revoke",                    "finance.adjustment.discounts_students.assign", True),
    *[("finance.adjustment.refund_request.initiate",          d, h) for d, h in _base()],
    *[("finance.adjustment.refund_amount.validate",           d, h) for d, h in _base()],
    ("finance.adjustment.refund_amount.validate",              "finance.adjustment.refund_request.initiate", True),
    *[("finance.adjustment.refund_workflow.approve",          d, h) for d, h in _base_audit()],
    ("finance.adjustment.refund_workflow.approve",             "finance.adjustment.refund_request.initiate", True),
    ("finance.adjustment.refund_workflow.approve",             "system.role_request.approve",                True),
    *[("finance.adjustment.refund_payment.execute",           d, h) for d, h in _base_audit()],
    ("finance.adjustment.refund_payment.execute",              "finance.adjustment.refund_workflow.approve", True),
    *[("finance.adjustment.billing_adjustments.apply",        d, h) for d, h in _base_audit()],
    *[("finance.adjustment.financial_entries.reverse",        d, h) for d, h in _base_audit()],
    ("finance.adjustment.financial_entries.reverse",           "finance.adjustment.billing_adjustments.apply", True),
    *[("finance.adjustment.adjustment_history.track",         d, h) for d, h in _base()],
    *[("finance.adjustment.stakeholders_adjustments.notify",  d, h) for d, h in _base()],
    *[("finance.adjustment.adjustment_limits.enforce",        d, h) for d, h in _base()],
    *[("finance.adjustment.adjustments_post_approval.lock",   d, h) for d, h in _base_audit()],
    *[("finance.adjustment.duplicate_refunds.prevent",        d, h) for d, h in _base()],
    *[("finance.adjustment.adjustment_reports.export",        d, h) for d, h in _base_export()],
    *[("finance.adjustment.discount_actions.audit",           d, h) for d, h in _base()],
    *[("finance.adjustment.refund_activities.audit",          d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 20: VENDOR MANAGEMENT
    # ------------------------------------------------------------------
    *[("procurement.vendor.vendor.register",              d, h) for d, h in _base_audit()],
    *[("procurement.vendor.vendor_registration.approve",  d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendor_registration.approve",     "procurement.vendor.vendor.register",     True),
    ("procurement.vendor.vendor_registration.approve",     "system.role_request.approve",             True),
    *[("procurement.vendor.vendors.categorize",           d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendors.categorize",              "procurement.vendor.vendor.register",     False),
    *[("procurement.vendor.vendor_profile.update",        d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendor_profile.update",           "procurement.vendor.vendor.register",     True),
    *[("procurement.vendor.vendor.deactivate",            d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendor.deactivate",               "procurement.vendor.vendor.register",     True),
    *[("procurement.vendor.vendor_performance.rate",      d, h) for d, h in _base_audit()],
    *[("procurement.vendor.vendor_history.track",         d, h) for d, h in _base()],
    *[("procurement.vendor.vendor_contracts.assign",      d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendor_contracts.assign",         "procurement.vendor.vendor_registration.approve", True),
    *[("procurement.vendor.vendor_documents.upload",      d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendor_documents.upload",         "procurement.vendor.vendor.register",     True),
    *[("procurement.vendor.vendor_compliance.validate",   d, h) for d, h in _base()],
    *[("procurement.vendor.vendor_directory.search",      d, h) for d, h in _base()],
    *[("procurement.vendor.vendor_list.export",           d, h) for d, h in _base_export()],
    *[("procurement.vendor.duplicate_vendors.detect",     d, h) for d, h in _base()],
    *[("procurement.vendor.vendor_records.lock",          d, h) for d, h in _base_audit()],
    *[("procurement.vendor.vendor_account.restore",       d, h) for d, h in _base_audit()],
    ("procurement.vendor.vendor_account.restore",          "procurement.vendor.vendor.deactivate",   True),
    *[("procurement.vendor.vendor_spend_summary.view",    d, h) for d, h in _base()],
    *[("procurement.vendor.vendor_payment_status.track",  d, h) for d, h in _base()],
    *[("procurement.vendor.vendor_changes.audit",         d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 21: PROCUREMENT REQUESTS & APPROVALS
    # ------------------------------------------------------------------
    *[("procurement.request.purchase_request.create",         d, h) for d, h in _base_audit()],
    *[("procurement.request.purchase_request.edit",           d, h) for d, h in _base_audit()],
    ("procurement.request.purchase_request.edit",              "procurement.request.purchase_request.create", True),
    *[("procurement.request.budget_availability.validate",    d, h) for d, h in _base()],
    *[("procurement.request.request_approval.submit",         d, h) for d, h in _base_audit()],
    ("procurement.request.request_approval.submit",            "procurement.request.purchase_request.create", True),
    *[("procurement.request.approval_workflow.route",         d, h) for d, h in _base_audit()],
    *[("procurement.request.purchase_request.approve",        d, h) for d, h in _base_audit()],
    ("procurement.request.purchase_request.approve",           "procurement.request.purchase_request.create", True),
    ("procurement.request.purchase_request.approve",           "system.role_request.approve",                 True),
    *[("procurement.request.purchase_request.reject",         d, h) for d, h in _base_audit()],
    ("procurement.request.purchase_request.reject",            "procurement.request.purchase_request.create", True),
    ("procurement.request.purchase_request.reject",            "system.role_request.approve",                 True),
    *[("procurement.request.high_value_requests.escalate",    d, h) for d, h in _base()],
    *[("procurement.request.approval_status.track",           d, h) for d, h in _base()],
    *[("procurement.request.approval_comments.add",           d, h) for d, h in _base_audit()],
    *[("procurement.request.spending_limits.enforce",         d, h) for d, h in _base()],
    *[("procurement.request.purchase_request.cancel",         d, h) for d, h in _base_audit()],
    ("procurement.request.purchase_request.cancel",            "procurement.request.purchase_request.create", True),
    *[("procurement.request.request_stakeholders.notify",     d, h) for d, h in _base()],
    *[("procurement.request.request_po.convert",              d, h) for d, h in _base_audit()],
    ("procurement.request.request_po.convert",                 "procurement.request.purchase_request.approve", True),
    *[("procurement.request.request_history.track",           d, h) for d, h in _base()],
    *[("procurement.request.request_data.export",             d, h) for d, h in _base_export()],
    *[("procurement.request.role_based_approvals.enforce",    d, h) for d, h in _base()],
    *[("procurement.request.request_actions.audit",           d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 22: PURCHASE ORDERS & DELIVERY
    # ------------------------------------------------------------------
    *[("procurement.purchase_order.purchase_order.generate",      d, h) for d, h in _base_audit()],
    ("procurement.purchase_order.purchase_order.generate",         "procurement.request.request_po.convert", True),
    *[("procurement.purchase_order.vendor_po.assign",             d, h) for d, h in _base_audit()],
    ("procurement.purchase_order.vendor_po.assign",                "procurement.purchase_order.purchase_order.generate", True),
    ("procurement.purchase_order.vendor_po.assign",                "procurement.vendor.vendor_registration.approve", True),
    *[("procurement.purchase_order.po_vendor.send",               d, h) for d, h in _base_audit()],
    ("procurement.purchase_order.po_vendor.send",                  "procurement.purchase_order.vendor_po.assign", True),
    *[("procurement.purchase_order.po_status.update",             d, h) for d, h in _base_audit()],
    *[("procurement.purchase_order.po_receipt.acknowledge",       d, h) for d, h in _base_audit()],
    *[("procurement.purchase_order.delivery_timeline.track",      d, h) for d, h in _base()],
    *[("procurement.purchase_order.goods_received.record",        d, h) for d, h in _base_audit()],
    *[("procurement.purchase_order.delivered_quantity.validate",  d, h) for d, h in _base()],
    *[("procurement.purchase_order.delivered_quality.validate",   d, h) for d, h in _base()],
    *[("procurement.purchase_order.delivered_items.reject",       d, h) for d, h in _base_audit()],
    *[("procurement.purchase_order.delivery_completion.approve",  d, h) for d, h in _base_audit()],
    ("procurement.purchase_order.delivery_completion.approve",     "procurement.purchase_order.goods_received.record", True),
    ("procurement.purchase_order.delivery_completion.approve",     "system.role_request.approve",                      True),
    *[("procurement.purchase_order.invoice_matching.trigger",     d, h) for d, h in _base_audit()],
    ("procurement.purchase_order.invoice_matching.trigger",        "procurement.purchase_order.delivery_completion.approve", True),
    *[("procurement.purchase_order.purchase_order.close",         d, h) for d, h in _base_audit()],
    ("procurement.purchase_order.purchase_order.close",            "procurement.purchase_order.invoice_matching.trigger", True),
    *[("procurement.purchase_order.purchase_order.cancel",        d, h) for d, h in _base_audit()],
    *[("procurement.purchase_order.po_documents.export",          d, h) for d, h in _base_export()],
    *[("procurement.purchase_order.po_history.track",             d, h) for d, h in _base()],
    *[("procurement.purchase_order.delivery_stakeholders.notify", d, h) for d, h in _base()],
    *[("procurement.purchase_order.po_changes.audit",             d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 23: INVENTORY & ASSET TRACKING
    # ------------------------------------------------------------------
    *[("inventory.inventory_item.register",       d, h) for d, h in _base_audit()],
    *[("inventory.inventory_category.assign",     d, h) for d, h in _base_audit()],
    ("inventory.inventory_category.assign",        "inventory.inventory_item.register", True),
    *[("inventory.inventory_quantity.track",      d, h) for d, h in _base()],
    *[("inventory.stock_levels.update",           d, h) for d, h in _base_audit()],
    ("inventory.stock_levels.update",              "inventory.inventory_item.register", True),
    *[("inventory.reorder_thresholds.set",        d, h) for d, h in _base_audit()],
    ("inventory.reorder_thresholds.set",           "inventory.inventory_item.register", True),
    *[("inventory.stock_alerts.generate",         d, h) for d, h in _base()],
    ("inventory.stock_alerts.generate",            "inventory.reorder_thresholds.set",  True),
    *[("inventory.asset_acquisition.record",      d, h) for d, h in _base_audit()],
    *[("inventory.asset_location.assign",         d, h) for d, h in _base_audit()],
    ("inventory.asset_location.assign",            "inventory.asset_acquisition.record", True),
    *[("inventory.asset_depreciation.track",      d, h) for d, h in _base()],
    ("inventory.asset_depreciation.track",         "inventory.asset_acquisition.record", True),
    *[("inventory.asset_disposal.record",         d, h) for d, h in _base_audit()],
    ("inventory.asset_disposal.record",            "inventory.asset_acquisition.record", True),
    *[("inventory.inventory_movements.audit",     d, h) for d, h in _base()],
    *[("inventory.stock_reconciliation.perform",  d, h) for d, h in _base_audit()],
    *[("inventory.inventory_records.lock",        d, h) for d, h in _base_audit()],
    *[("inventory.inventory_reports.export",      d, h) for d, h in _base_export()],
    *[("inventory.asset_history.track",           d, h) for d, h in _base()],
    *[("inventory.assets_departments.assign",     d, h) for d, h in _base_audit()],
    ("inventory.assets_departments.assign",        "inventory.asset_acquisition.record", True),
    *[("inventory.inventory_permissions.enforce", d, h) for d, h in _base()],
    *[("inventory.asset_records.archive",         d, h) for d, h in _base_audit()],
    ("inventory.asset_records.archive",            "inventory.asset_acquisition.record", True),

    # ------------------------------------------------------------------
    # MODULE 24: DASHBOARDS & KPIs
    # ------------------------------------------------------------------
    *[("analytics.dashboard.school_overview_dashboard.view", d, h) for d, h in _base()],
    *[("analytics.dashboard.academic_performance_kpis.view",      d, h) for d, h in _base()],
    *[("analytics.dashboard.attendance_kpis.view",                d, h) for d, h in _base()],
    *[("analytics.dashboard.financial_kpis.view",                 d, h) for d, h in _base()],
    *[("analytics.dashboard.procurement_kpis.view",               d, h) for d, h in _base()],
    *[("analytics.dashboard.dashboard_metrics.filter",            d, h) for d, h in _base()],
    ("analytics.dashboard.dashboard_metrics.filter",               "analytics.dashboard.school_overview_dashboard.view", False),
    *[("analytics.dashboard.kpis.drilldown",                      d, h) for d, h in _base()],
    *[("analytics.dashboard.period_performance.compare",          d, h) for d, h in _base()],
    *[("analytics.dashboard.dashboard_widgets.configure",         d, h) for d, h in _base_audit()],
    *[("analytics.dashboard.custom_dashboards.save",              d, h) for d, h in _base_audit()],
    *[("analytics.dashboard.dashboard_access_control.enforce",    d, h) for d, h in _base()],
    *[("analytics.dashboard.dashboard_data.refresh",              d, h) for d, h in _base()],
    *[("analytics.dashboard.dashboard_views.export",              d, h) for d, h in _base_export()],
    *[("analytics.dashboard.dashboard_usage.track",               d, h) for d, h in _base()],
    *[("analytics.dashboard.dashboard_changes.audit",             d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 25: OPERATIONAL REPORTS & EXPORT
    # ------------------------------------------------------------------
    *[("reporting.operational.attendance_reports.generate",   d, h) for d, h in _base()],
    *[("reporting.operational.academic_reports.generate",     d, h) for d, h in _base()],
    *[("reporting.operational.financial_reports.generate",    d, h) for d, h in _base()],
    *[("reporting.operational.procurement_reports.generate",  d, h) for d, h in _base()],
    *[("reporting.operational.user_activity_reports.generate",d, h) for d, h in _base()],
    *[("reporting.operational.report_generation.schedule",    d, h) for d, h in _base()],
    *[("reporting.operational.report_parameters.filter",      d, h) for d, h in _base()],
    *[("reporting.operational.reports.preview",               d, h) for d, h in _base()],
    *[("reporting.operational.reports_pdf.export",            d, h) for d, h in _base_export()],
    *[("reporting.operational.reports_excel.export",          d, h) for d, h in _base_export()],
    *[("reporting.operational.reports_email.share",           d, h) for d, h in _base()],
    ("reporting.operational.reports_email.share",              "system.data_masking.apply", False),
    *[("reporting.operational.generated_reports.archive",     d, h) for d, h in _base_audit()],
    *[("reporting.operational.report_history.track",          d, h) for d, h in _base()],
    *[("reporting.operational.report_access_control.enforce", d, h) for d, h in _base()],
    *[("reporting.operational.report_generation.audit",       d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 26: STUDENT PORTAL
    # ------------------------------------------------------------------
    *[("student_portal.student_login.create",         d, h) for d, h in _base_audit()],
    *[("student_portal.class_timetable.view",         d, h) for d, h in _base()],
    *[("student_portal.attendance_summary.view",      d, h) for d, h in _base()],
    *[("student_portal.academic_results.view",        d, h) for d, h in _base()],
    *[("student_portal.learning_materials.access",    d, h) for d, h in _base()],
    *[("student_portal.announcements.receive",        d, h) for d, h in _base()],
    *[("student_portal.fee_status.view",              d, h) for d, h in _base()],
    *[("student_portal.receipts.download",            d, h) for d, h in _base()],
    ("student_portal.receipts.download",               "system.data_masking.apply", False),
    *[("student_portal.teachers.message",             d, h) for d, h in _base()],
    ("student_portal.teachers.message",                "communication.internal_message.send", False),
    *[("student_portal.profile_details.update",       d, h) for d, h in _base_audit()],
    *[("student_portal.academic_progress.track",      d, h) for d, h in _base()],
    *[("student_portal.student_permissions.enforce",  d, h) for d, h in _base()],
    *[("student_portal.student_activity.audit",       d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # MODULE 27: PARENT/GUARDIAN PORTAL
    # ------------------------------------------------------------------
    *[("parent_portal.parent_login.create",           d, h) for d, h in _base_audit()],
    *[("parent_portal.child_attendance.view",         d, h) for d, h in _base()],
    *[("parent_portal.child_academic_results.view",   d, h) for d, h in _base()],
    *[("parent_portal.fee_status.view",               d, h) for d, h in _base()],
    *[("parent_portal.fee_payments.make",             d, h) for d, h in _base_audit()],
    ("parent_portal.fee_payments.make",                "parent_portal.fee_status.view", True),
    *[("parent_portal.payment_receipts.download",     d, h) for d, h in _base()],
    ("parent_portal.payment_receipts.download",        "system.data_masking.apply", False),
    *[("parent_portal.announcements.receive",         d, h) for d, h in _base()],
    *[("parent_portal.teachers_admin.message",        d, h) for d, h in _base()],
    ("parent_portal.teachers_admin.message",           "communication.internal_message.send", False),
    *[("parent_portal.child_timetable.view",          d, h) for d, h in _base()],
    *[("parent_portal.contact_details.update",        d, h) for d, h in _base_audit()],
    *[("parent_portal.multiple_children.manage",      d, h) for d, h in _base()],
    *[("parent_portal.portal_activity.audit",         d, h) for d, h in _base()],

    # ------------------------------------------------------------------
    # DATA EXPORT (cross-cutting)
    # ------------------------------------------------------------------
    *[("data_export.student_data.export",             d, h) for d, h in _base_export()],
    ("data_export.student_data.export",                "student.student_data.export", False),
    *[("data_export.staff_data.export",               d, h) for d, h in _base_export()],
    ("data_export.staff_data.export",                  "staff.staff_data.export", False),
    *[("data_export.attendance_data.export",          d, h) for d, h in _base_export()],
    *[("data_export.academic_results.export",         d, h) for d, h in _base_export()],
    *[("data_export.financial_records.export",        d, h) for d, h in _base_export()],
    *[("data_export.procurement_records.export",      d, h) for d, h in _base_export()],
    *[("data_export.audit_logs.export",               d, h) for d, h in _base_export()],
    ("data_export.audit_logs.export",                  "system.audit.view", True),
    *[("data_export.export_formats.configure",        d, h) for d, h in _base_audit()],
    *[("data_export.data_access_permissions.enforce", d, h) for d, h in _base()],
    *[("data_export.data_masking_rules.apply",        d, h) for d, h in _base_audit()],
    *[("data_export.automated_exports.schedule",      d, h) for d, h in _base()],
    *[("data_export.secure_download_links.generate",  d, h) for d, h in _base()],
    *[("data_export.export_requests.track",           d, h) for d, h in _base()],
    *[("data_export.export_files.expire",             d, h) for d, h in _base_audit()],
    *[("data_export.data_access_events.audit",        d, h) for d, h in _base()],
]


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Seed all Permission and PermissionDependency records across all 27 "
        "X Vision Systems modules. Safe to run multiple times (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be seeded without writing to the database.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "DELETE all existing Permission and PermissionDependency records "
                "before seeding. Use with caution in production."
            ),
        )

    def handle(self, *args, **options):
        from vs_rbac.models import Permission, PermissionDependency  # noqa: lazy import

        dry_run: bool = options["dry_run"]
        reset: bool = options["reset"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes will occur.\n"))

        # ------------------------------------------------------------------
        # Optional reset
        # ------------------------------------------------------------------
        if reset and not dry_run:
            self.stdout.write(self.style.WARNING(
                "Resetting all Permission and PermissionDependency records..."
            ))
            with transaction.atomic():
                PermissionDependency.objects.all().delete()
                Permission.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("Reset complete.\n"))

        # ------------------------------------------------------------------
        # PHASE 1: Seed Permissions
        # ------------------------------------------------------------------
        self.stdout.write("Phase 1 — Seeding permissions...")

        created_count = 0
        skipped_count = 0

        if not dry_run:
            with transaction.atomic():
                for key, module_key, action, description in PERMISSIONS:
                    _, created = Permission.objects.get_or_create(
                        key=key,
                        defaults={
                            "module_key": module_key,
                            "action": action,
                            "description": description,
                            "is_active": True,
                        },
                    )
                    if created:
                        created_count += 1
                    else:
                        skipped_count += 1
        else:
            # Dry run: just count
            existing_keys = set(Permission.objects.values_list("key", flat=True))
            for key, *_ in PERMISSIONS:
                if key in existing_keys:
                    skipped_count += 1
                else:
                    created_count += 1

        self.stdout.write(
            f"  Permissions: {created_count} created, {skipped_count} already existed "
            f"(total defined: {len(PERMISSIONS)})"
        )

        # ------------------------------------------------------------------
        # PHASE 2: Seed Dependencies
        # ------------------------------------------------------------------
        self.stdout.write("Phase 2 — Seeding permission dependencies...")

        # Build a key→id lookup for fast reference
        if not dry_run:
            perm_map: dict[str, int] = dict(
                Permission.objects.values_list("key", "key")
            )
        else:
            # Use placeholder ids for dry run reporting
            perm_map = {key: idx for idx, (key, *_) in enumerate(PERMISSIONS)}

        # Validate all dependency keys exist in the permission catalog
        defined_keys = {key for key, *_ in PERMISSIONS}
        missing_refs: list[str] = []

        for perm_key, dep_key, _ in DEPENDENCIES:
            if perm_key not in defined_keys:
                missing_refs.append(f"  UNKNOWN permission key: {perm_key}")
            if dep_key not in defined_keys:
                missing_refs.append(f"  UNKNOWN dependency key: {dep_key} (referenced by {perm_key})")

        if missing_refs:
            self.stderr.write(self.style.ERROR(
                "Dependency validation failed — unknown keys found:\n" +
                "\n".join(missing_refs)
            ))
            raise CommandError("Fix the unknown keys above before seeding.")

        dep_created = 0
        dep_skipped = 0

        if not dry_run:
            # Collect all dependency objects for bulk insert
            dep_objects = []
            seen_pairs: set[tuple[int, int]] = set()

            for perm_key, dep_key, is_hard in DEPENDENCIES:
                perm_id = perm_map.get(perm_key)
                dep_id = perm_map.get(dep_key)

                if perm_id is None or dep_id is None:
                    # Should not happen given validation above
                    continue

                pair = (perm_id, dep_id)
                if pair in seen_pairs:
                    # Deduplicate within the catalog itself
                    continue
                seen_pairs.add(pair)

                dep_objects.append(
                    PermissionDependency(
                        permission_id=perm_id,
                        depends_on_id=dep_id,
                    )
                )

            with transaction.atomic():
                # Get already-existing pairs to calculate created vs skipped
                existing_pairs: set[tuple[int, int]] = set(
                    PermissionDependency.objects.values_list(
                        "permission_id", "depends_on_id"
                    )
                )

                new_deps = [
                    d for d in dep_objects
                    if (d.permission_id, d.depends_on_id) not in existing_pairs
                ]
                dep_skipped = len(dep_objects) - len(new_deps)

                if new_deps:
                    PermissionDependency.objects.bulk_create(
                        new_deps,
                        ignore_conflicts=True,
                        batch_size=500,
                    )
                    dep_created = len(new_deps)

        else:
            # Dry run count
            dep_created = len(DEPENDENCIES)

        self.stdout.write(
            f"  Dependencies: {dep_created} created, {dep_skipped} already existed "
            f"(total defined: {len(DEPENDENCIES)})"
        )

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN complete. Would seed {len(PERMISSIONS)} permissions "
                f"and {len(DEPENDENCIES)} dependency edges."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Seed complete.\n"
                f"  Permissions : {len(PERMISSIONS)} total defined\n"
                f"  Dependencies: {len(DEPENDENCIES)} total defined\n"
                "Run with --dry-run to preview without writing."
            ))