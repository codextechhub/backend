# CodeX Vision Permission Key Registry

Developer-readable markdown registry of permission keys derived from the CodeX Vision module and PRD breakdown.

Each entry contains only:
- `key` — the RBAC permission key to seed or check in code
- `action` — the normalized verb
- `capability` — what the permission allows

---

## Global Platform Guards

### `system.session.access.authenticate`
- **Action:** `authenticate`
- **Capability:** Allows a signed-in user session to be established before any protected workflow is used.

### `system.authenticated.access`
- **Action:** `access`
- **Capability:** Allows access only after the request is authenticated.

### `system.api.access`
- **Action:** `access`
- **Capability:** Allows access to protected API endpoints when transport, auth, and client checks pass.

### `system.tenant_context.require`
- **Action:** `require`
- **Capability:** Requires every tenant-scoped request to carry a valid institution context.

### `system.tenant_boundary.enforce`
- **Action:** `enforce`
- **Capability:** Prevents cross-institution reads, writes, or reference leakage.

### `system.mfa.enforce`
- **Action:** `enforce`
- **Capability:** Requires multi-factor authentication for privileged or sensitive access.

### `system.mfa.reset`
- **Action:** `reset`
- **Capability:** Allows an authorized operator to reset a user MFA enrollment.

### `system.password.policy.enforce`
- **Action:** `enforce`
- **Capability:** Requires passwords to satisfy the configured complexity and rotation policy.

### `system.token.refresh`
- **Action:** `refresh`
- **Capability:** Allows a refresh token to mint a new access token under policy.

### `system.rate_limit.login.enforce`
- **Action:** `enforce`
- **Capability:** Applies login throttling after repeated failed authentication attempts.

### `system.session.view`
- **Action:** `view`
- **Capability:** Allows viewing active sessions, devices, IP data, and last activity.

### `system.session.force_logout`
- **Action:** `force`
- **Capability:** Allows a privileged actor to revoke sessions and sign a user out.

### `system.audit.write`
- **Action:** `write`
- **Capability:** Requires sensitive state changes to emit an immutable audit record.

### `system.audit.view`
- **Action:** `view`
- **Capability:** Allows authorized users to inspect the audit trail.

### `system.audit.search`
- **Action:** `search`
- **Capability:** Allows searching audit data by actor, date, entity, or event type.

### `system.audit.export`
- **Action:** `export`
- **Capability:** Allows export of audit results to approved formats under policy.

### `system.configuration.view`
- **Action:** `view`
- **Capability:** Allows viewing platform and institution configuration values.

### `system.configuration.update`
- **Action:** `update`
- **Capability:** Allows changing configuration values after validation and audit checks.

### `system.feature_flag.view`
- **Action:** `view`
- **Capability:** Allows reading current feature flag state and rollout scope.

### `system.feature_flag.toggle`
- **Action:** `toggle`
- **Capability:** Allows enabling or disabling a feature flag.

### `system.feature_flag.schedule`
- **Action:** `schedule`
- **Capability:** Allows scheduling future activation or deactivation of a flag.

### `system.health.view`
- **Action:** `view`
- **Capability:** Allows access to platform health, provisioning, and usage indicators.

### `system.security_alert.view`
- **Action:** `view`
- **Capability:** Allows inspection of failed-auth, anomaly, and security alert events.

### `system.support.impersonation.impersonate`
- **Action:** `impersonate`
- **Capability:** Allows audited user impersonation for support and troubleshooting.

### `system.download.secure_link.generate`
- **Action:** `generate`
- **Capability:** Allows generation of expiring secure download links for exports.

### `system.data_masking.apply`
- **Action:** `apply`
- **Capability:** Allows masking of sensitive fields in exports, reports, and previews.

### `system.export.schedule`
- **Action:** `schedule`
- **Capability:** Allows automated recurring export jobs to be configured.

### `system.export.expiry.enforce`
- **Action:** `enforce`
- **Capability:** Allows export files to be expired and revoked after policy windows.

### `system.role_request.approve`
- **Action:** `approve`
- **Capability:** Allows privileged staff to approve role change requests.

### `system.role_request.deny`
- **Action:** `deny`
- **Capability:** Allows privileged staff to deny role change requests.

### `system.approval.override.super_admin`
- **Action:** `override`
- **Capability:** Allows Super Admin override on exceptional guarded operations.

## TIER 1 — CORE PLATFORM MODULES

### Institution Management
_Namespace: `institution`_

#### `institution.institution.create`
- **Action:** `create`
- **Capability:** Create Institution alongside Subsidiaries.

#### `institution.institution_slug.generate`
- **Action:** `generate`
- **Capability:** Auto-Generate Institution Slug.

#### `institution.database_schema.provision`
- **Action:** `provision`
- **Capability:** Provision Institution Database Schema.

#### `institution.localization.assign`
- **Action:** `assign`
- **Capability:** Assign Institution Region & Localization.

#### `institution.lifecycle.configure`
- **Action:** `configure`
- **Capability:** Configure Institution Lifecycle States.

#### `institution.module_access.toggle`
- **Action:** `toggle`
- **Capability:** Enable / Disable Modules Per Institution (Eg

#### `institution.primary_administrator.assign`
- **Action:** `assign`
- **Capability:** Assign Primary Institution Administrator.

#### `institution.branding_assets.upload`
- **Action:** `upload`
- **Capability:** Upload Institution Branding Assets.

#### `institution.metadata.update`
- **Action:** `update`
- **Capability:** Update Institution Metadata.

#### `institution.uniqueness.validate`
- **Action:** `validate`
- **Capability:** Validate Institution Uniqueness.

#### `institution.data_isolation.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Institution Data Isolation.

#### `institution.access_state.suspend`
- **Action:** `suspend`
- **Capability:** Suspend Institution.

#### `institution.access_state.reactivate`
- **Action:** `reactivate`
- **Capability:** Reactivate Suspended Institution.

#### `institution.deletion_state.soft_delete`
- **Action:** `soft_delete`
- **Capability:** Soft Delete Institution.

#### `institution.deletion_state.hard_delete`
- **Action:** `hard_delete`
- **Capability:** Hard Delete Institution.

#### `institution.configuration.reset`
- **Action:** `reset`
- **Capability:** Reset Institution Configuration.

#### `institution.health_status.view`
- **Action:** `view`
- **Capability:** View Institution Health Status.

#### `institution.creation_audit_logs.track`
- **Action:** `track`
- **Capability:** Track Institution Creation Audit Logs.

#### `institution.feature_flags.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Institution-Level Feature Flags.

#### `institution.provisioning.rollback`
- **Action:** `rollback`
- **Capability:** Rollback Failed Institution Provisioning.

### Vision Admin Console (Internal Backoffice)
_Namespace: `vision_admin`_

#### `vision_admin.session.authenticate`
- **Action:** `authenticate`
- **Capability:** Authenticate Vision Admin User.

#### `vision_admin.institution_dashboard.view`
- **Action:** `view`
- **Capability:** View All Institutions Dashboard.

#### `vision_admin.institution_configuration.edit`
- **Action:** `edit`
- **Capability:** Edit Institution Configuration.

#### `vision_admin.access_state.suspend_manage`
- **Action:** `suspend_manage`
- **Capability:** Suspend / Unsuspend Institution.

#### `vision_admin.data_configuration.reset`
- **Action:** `reset`
- **Capability:** Reset Institution Data/Configuration.

#### `vision_admin.institution_provisioning_pipeline.monitor`
- **Action:** `monitor`
- **Capability:** Monitor Institution Provisioning Pipeline.

#### `vision_admin.failed_provisioning_steps.retry`
- **Action:** `retry`
- **Capability:** Retry Failed Provisioning Steps.

#### `vision_admin.institution_usage_metrics.view`
- **Action:** `view`
- **Capability:** View Institution Usage Metrics.

#### `vision_admin.institution_audit_logs.access`
- **Action:** `access`
- **Capability:** Access Institution Audit Logs.

#### `vision_admin.institution_user.impersonate`
- **Action:** `impersonate`
- **Capability:** Impersonate Institution User.

#### `vision_admin.role_change_request.manage`
- **Action:** `manage`
- **Capability:** Manage Role Change Requests.

#### `vision_admin.import_jobs.review`
- **Action:** `review`
- **Capability:** Review Import Jobs & Errors.

#### `vision_admin.data_fix.apply`
- **Action:** `apply`
- **Capability:** Apply Manual Data Fixes.

#### `vision_admin.feature_flags.toggle`
- **Action:** `toggle`
- **Capability:** Toggle Feature Flags Per Institution.

#### `vision_admin.health_metrics.view`
- **Action:** `view`
- **Capability:** View System Health Metrics.

#### `vision_admin.admin_roles.manage`
- **Action:** `manage`
- **Capability:** Manage Vision Admin Roles.

#### `vision_admin.mfa.enforce`
- **Action:** `enforce`
- **Capability:** Enforce MFA for Vision Staff.

#### `vision_admin.security_alerts.view`
- **Action:** `view`
- **Capability:** View Security Alerts.

#### `vision_admin.admin_actions.log`
- **Action:** `log`
- **Capability:** Log All Admin Actions.

### User Identity, Accounts & Authentication
_Namespace: `identity`_

#### `identity.user_account.create`
- **Action:** `create`
- **Capability:** Create User Account.

#### `identity.user_email.invite`
- **Action:** `invite`
- **Capability:** Invite User via Email.

#### `identity.user_account.activate`
- **Action:** `activate`
- **Capability:** Activate User Account.

#### `identity.password_policy.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Password Policy.

#### `identity.access_token.refresh`
- **Action:** `refresh`
- **Capability:** Refresh Access Token.

#### `identity.user_account.lock`
- **Action:** `lock`
- **Capability:** Lock User Account.

#### `identity.user_account.unlock`
- **Action:** `unlock`
- **Capability:** Unlock User Account.

#### `identity.login_sessions.track`
- **Action:** `track`
- **Capability:** Track Login Sessions.

#### `identity.user_logout.force`
- **Action:** `force`
- **Capability:** Force User Logout.

#### `identity.user_password.reset`
- **Action:** `reset`
- **Capability:** Reset User Password.

#### `identity.email_address.verify`
- **Action:** `verify`
- **Capability:** Verify Email Address.

#### `identity.phone_number.verify`
- **Action:** `verify`
- **Capability:** Verify Phone Number.

#### `identity.institution_aware_login.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Institution-Aware Login.

#### `identity.authentication_events.log`
- **Action:** `log`
- **Capability:** Log Authentication Events.

### Roles, Permissions & Access Control
_Namespace: `rbac`_

#### `rbac.role_template.create`
- **Action:** `create`
- **Capability:** Create Role Template.

#### `rbac.role_user.assign`
- **Action:** `assign`
- **Capability:** Assign Role to User.

#### `rbac.role_permissions.update`
- **Action:** `update`
- **Capability:** Update Role Permissions.

#### `rbac.role_based_access_control.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Role-Based Access Control.

#### `rbac.cross_module_access.restrict`
- **Action:** `restrict`
- **Capability:** Restrict Cross-Module Access.

#### `rbac.permission_dependencies.validate`
- **Action:** `validate`
- **Capability:** Validate Permission Dependencies.

#### `rbac.permission_changes_instantly.apply`
- **Action:** `apply`
- **Capability:** Apply Permission Changes Instantly.

#### `rbac.role_change.request`
- **Action:** `request`
- **Capability:** Request Role Change.

#### `rbac.role_change.approve`
- **Action:** `approve`
- **Capability:** Approve Role Change.

#### `rbac.role_change_request.deny`
- **Action:** `deny`
- **Capability:** Deny Role Change Request.

#### `rbac.multiple_roles_user.assign`
- **Action:** `assign`
- **Capability:** Assign Multiple Roles to User.

#### `rbac.role.deactivate`
- **Action:** `deactivate`
- **Capability:** Deactivate Role.

#### `rbac.role_template.archive`
- **Action:** `archive`
- **Capability:** Archive Role Template.

#### `rbac.api_level_permission_checks.enforce`
- **Action:** `enforce`
- **Capability:** Enforce API-Level Permission Checks.

#### `rbac.role_changes.audit`
- **Action:** `audit`
- **Capability:** Audit Role Changes.

#### `rbac.compliance_constraints.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Compliance Constraints.

#### `rbac.permission_matrix.view`
- **Action:** `view`
- **Capability:** View Permission Matrix.

#### `rbac.previous_role_state.restore`
- **Action:** `restore`
- **Capability:** Restore Previous Role State.

#### `rbac.critical_system_roles.lock`
- **Action:** `lock`
- **Capability:** Lock Critical System Roles.

### Audit Logging & Compliance
_Namespace: `audit`_

#### `audit.user_creation_events.log`
- **Action:** `log`
- **Capability:** Log User Creation Events.

#### `audit.authentication_events.log`
- **Action:** `log`
- **Capability:** Log Authentication Events.

#### `audit.data_import_actions.log`
- **Action:** `log`
- **Capability:** Log Data Import Actions.

#### `audit.configuration_changes.log`
- **Action:** `log`
- **Capability:** Log Configuration Changes.

#### `audit.financial_transactions.log`
- **Action:** `log`
- **Capability:** Log Financial Transactions.

#### `audit.procurement_actions.log`
- **Action:** `log`
- **Capability:** Log Procurement Actions.

#### `audit.permission_changes.log`
- **Action:** `log`
- **Capability:** Log Permission Changes.

#### `audit.immutable_log_storage.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Immutable Log Storage.

#### `audit.audit_events.timestamp`
- **Action:** `timestamp`
- **Capability:** Timestamp All Audit Events.

#### `audit.actions_actor.attribute`
- **Action:** `attribute`
- **Capability:** Attribute Actions to Actor.

#### `audit.audit_logs.search`
- **Action:** `search`
- **Capability:** Search Audit Logs.

#### `audit.logs_by_action_type.filter`
- **Action:** `filter`
- **Capability:** Filter Logs by Action Type.

#### `audit.logs_by_date_range.filter`
- **Action:** `filter`
- **Capability:** Filter Logs by Date Range.

#### `audit.audit_logs.export`
- **Action:** `export`
- **Capability:** Export Audit Logs.

#### `audit.log_retrieval.paginate`
- **Action:** `paginate`
- **Capability:** Paginate Log Retrieval.

#### `audit.audit_trail_entity.view`
- **Action:** `view`
- **Capability:** Display Audit Trail per Entity.

### System Configuration & Feature Flags
_Namespace: `system_config`_

#### `system_config.global_system_settings.view`
- **Action:** `view`
- **Capability:** View Global System Settings.

#### `system_config.global_system_settings.update`
- **Action:** `update`
- **Capability:** Update Global System Settings.

#### `system_config.configuration_key.create`
- **Action:** `create`
- **Capability:** Create Configuration Key.

#### `system_config.configuration_key.edit`
- **Action:** `edit`
- **Capability:** Edit Configuration Key.

#### `system_config.configuration_key.delete`
- **Action:** `delete`
- **Capability:** Delete Configuration Key.

#### `system_config.deleted_configuration_key.restore`
- **Action:** `restore`
- **Capability:** Restore Deleted Configuration Key.

#### `system_config.configuration_schema.validate`
- **Action:** `validate`
- **Capability:** Validate Configuration Schema.

#### `system_config.configuration_access_permissions.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Configuration Access Permissions.

#### `system_config.configuration_change_history.track`
- **Action:** `track`
- **Capability:** Track Configuration Change History.

#### `system_config.configuration_changes.rollback`
- **Action:** `rollback`
- **Capability:** Rollback Configuration Changes.

#### `system_config.feature_flag.create`
- **Action:** `create`
- **Capability:** Create Feature Flag.

#### `system_config.feature_flag_globally.enable`
- **Action:** `enable`
- **Capability:** Enable Feature Flag Globally.

#### `system_config.feature_flag_globally.disable`
- **Action:** `disable`
- **Capability:** Disable Feature Flag Globally.

#### `system_config.feature_flag_institution.enable`
- **Action:** `enable`
- **Capability:** Enable Feature Flag Per Institution.

#### `system_config.feature_flag_institution.disable`
- **Action:** `disable`
- **Capability:** Disable Feature Flag Per Institution.

#### `system_config.feature_flag_activation.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Feature Flag Activation.

#### `system_config.feature_flag_deactivation.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Feature Flag Deactivation.

#### `system_config.flags_specific_roles.restrict`
- **Action:** `restrict`
- **Capability:** Restrict Flags to Specific Roles.

#### `system_config.feature_flag_changes.audit`
- **Action:** `audit`
- **Capability:** Audit Feature Flag Changes.

#### `system_config.conflicting_flag_rules.detect`
- **Action:** `detect`
- **Capability:** Detect Conflicting Flag Rules.

#### `system_config.configuration_and_feature_flag_settings.export`
- **Action:** `export`
- **Capability:** Export Configuration & Feature Flag Settings.

---

## TIER 2 — ONBOARDING & DATA FOUNDATION

### Institution Onboarding Module
_Namespace: `onboarding`_

#### `onboarding.institution_onboarding.initiate`
- **Action:** `initiate`
- **Capability:** Initiate Institution Onboarding.

#### `onboarding.institution_metadata.capture`
- **Action:** `capture`
- **Capability:** Capture Institution Metadata.

#### `onboarding.academic_structure.configure`
- **Action:** `configure`
- **Capability:** Configure Academic Structure.

#### `onboarding.financial_structure.configure`
- **Action:** `configure`
- **Capability:** Configure Financial Structure.

#### `onboarding.procurement_structure.configure`
- **Action:** `configure`
- **Capability:** Configure Procurement Structure.

#### `onboarding.default_roles.assign`
- **Action:** `assign`
- **Capability:** Assign Default Roles.

#### `onboarding.default_permissions.seed`
- **Action:** `seed`
- **Capability:** Seed Default Permissions.

#### `onboarding.initial_datasets.upload`
- **Action:** `upload`
- **Capability:** Upload Initial Datasets.

#### `onboarding.onboarding_completeness.validate`
- **Action:** `validate`
- **Capability:** Validate Onboarding Completeness.

#### `onboarding.onboarding_progress.track`
- **Action:** `track`
- **Capability:** Track Onboarding Progress.

#### `onboarding.go_live_on_validation_errors.block`
- **Action:** `block`
- **Capability:** Block Go-Live on Validation Errors.

#### `onboarding.training_session.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Training Session.

#### `onboarding.onboarding_checklist.generate`
- **Action:** `generate`
- **Capability:** Generate Onboarding Checklist.

#### `onboarding.onboarding_actions.log`
- **Action:** `log`
- **Capability:** Log Onboarding Actions.

#### `onboarding.incomplete_onboarding.rollback`
- **Action:** `rollback`
- **Capability:** Rollback Incomplete Onboarding.

#### `onboarding.post_go_live_activity.monitor`
- **Action:** `monitor`
- **Capability:** Monitor Post-Go-Live Activity.

#### `onboarding.onboarding_issues.escalate`
- **Action:** `escalate`
- **Capability:** Escalate Onboarding Issues.

### Data Import & Validation Engine
_Namespace: `data_import`_

#### `data_import.csv_file.upload`
- **Action:** `upload`
- **Capability:** Upload CSV File.

#### `data_import.excel_file.upload`
- **Action:** `upload`
- **Capability:** Upload Excel File.

#### `data_import.dataset_type.detect`
- **Action:** `detect`
- **Capability:** Detect Dataset Type.

#### `data_import.file_structure.validate`
- **Action:** `validate`
- **Capability:** Validate File Structure.

#### `data_import.mandatory_fields.validate`
- **Action:** `validate`
- **Capability:** Validate Mandatory Fields.

#### `data_import.duplicate_records.detect`
- **Action:** `detect`
- **Capability:** Detect Duplicate Records.

#### `data_import.cross_entity_references.validate`
- **Action:** `validate`
- **Capability:** Validate Cross-Entity References.

#### `data_import.auto_mapping_rules.apply`
- **Action:** `apply`
- **Capability:** Apply Auto-Mapping Rules.

#### `data_import.field_mapping.map`
- **Action:** `map`
- **Capability:** Allow Manual Field Mapping.

#### `data_import.validation_error_report.generate`
- **Action:** `generate`
- **Capability:** Generate Validation Error Report.

#### `data_import.errors_vs_warnings.classify`
- **Action:** `classify`
- **Capability:** Classify Errors vs Warnings.

#### `data_import.import_on_critical_errors.block`
- **Action:** `block`
- **Capability:** Block Import on Critical Errors.

#### `data_import.background_import_job.execute`
- **Action:** `execute`
- **Capability:** Execute Background Import Job.

#### `data_import.import_progress.track`
- **Action:** `track`
- **Capability:** Track Import Progress.

#### `data_import.error_import_rows.edit`
- **Action:** `edit`
- **Capability:** Edit Error Import Rows.

#### `data_import.failed_imports.rollback`
- **Action:** `rollback`
- **Capability:** Rollback Failed Imports.

#### `data_import.data_changes.log`
- **Action:** `log`
- **Capability:** Log Data Changes.

#### `data_import.large_dataset_imports.support`
- **Action:** `support`
- **Capability:** Support Large Dataset Imports.

#### `data_import.import_history.store`
- **Action:** `store`
- **Capability:** Store Import History.

#### `data_import.admin_on_completion.notify`
- **Action:** `notify`
- **Capability:** Notify Admin on Completion.

---

## TIER 3 — ACADEMIC OPERATIONS (FOUNDATION LIST)

### Student Management
_Namespace: `student`_

#### `student.student_profile.create`
- **Action:** `create`
- **Capability:** Create Student Profile.

#### `student.student_records.update`
- **Action:** `update`
- **Capability:** Update Student Records.

#### `student.student_class.assign`
- **Action:** `assign`
- **Capability:** Assign Student to Class.

#### `student.student_between_terms_sessions.promote`
- **Action:** `promote`
- **Capability:** Promote Student Between Terms/Sessions.

#### `student.student_records.archive`
- **Action:** `archive`
- **Capability:** Archive Student Records.

#### `student.archived_students.restore`
- **Action:** `restore`
- **Capability:** Restore Archived Students.

#### `student.student_status.manage`
- **Action:** `manage`
- **Capability:** Manage Student Status.

#### `student.student_documents.upload`
- **Action:** `upload`
- **Capability:** Upload Student Documents.

#### `student.student_identity.validate`
- **Action:** `validate`
- **Capability:** Validate Student Identity.

#### `student.enrollment_history.track`
- **Action:** `track`
- **Capability:** Track Enrollment History.

#### `student.parent_guardian_information.add`
- **Action:** `add`
- **Capability:** Add Parent/Guardian Information.

#### `student.data_privacy_rules.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Data Privacy Rules.

#### `student.student_records.search`
- **Action:** `search`
- **Capability:** Search Student Records.

#### `student.student_data.export`
- **Action:** `export`
- **Capability:** Export Student Data.

#### `student.student_record_changes.log`
- **Action:** `log`
- **Capability:** Log Student Record Changes.

### Staff Management
_Namespace: `staff`_

#### `staff.staff_profile.create`
- **Action:** `create`
- **Capability:** Create Staff Profile.

#### `staff.staff_role.assign`
- **Action:** `assign`
- **Capability:** Assign Staff Role.

#### `staff.staff_department.assign`
- **Action:** `assign`
- **Capability:** Assign Staff Department.

#### `staff.staff_account.activate`
- **Action:** `activate`
- **Capability:** Activate Staff Account.

#### `staff.staff_account.deactivate`
- **Action:** `deactivate`
- **Capability:** Deactivate Staff Account.

#### `staff.staff_department.transfer`
- **Action:** `transfer`
- **Capability:** Transfer Staff Department.

#### `staff.staff_activity.track`
- **Action:** `track`
- **Capability:** Track Staff Activity.

#### `staff.staff_permissions.manage`
- **Action:** `manage`
- **Capability:** Manage Staff Permissions.

#### `staff.staff_documents.upload`
- **Action:** `upload`
- **Capability:** Upload Staff Documents.

#### `staff.staff_records.search`
- **Action:** `search`
- **Capability:** Search Staff Records.

#### `staff.staff_data.export`
- **Action:** `export`
- **Capability:** Export Staff Data.

#### `staff.staff_actions.log`
- **Action:** `log`
- **Capability:** Log Staff Actions.

#### `staff.role_constraints.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Role Constraints.

#### `staff.staff_account.restore`
- **Action:** `restore`
- **Capability:** Restore Staff Account.

#### `staff.staff_record.archive`
- **Action:** `archive`
- **Capability:** Archive Staff Record.

### Academic Structure (Classes, Programs)
_Namespace: `academic_structure`_

#### `academic_structure.academic_program.create`
- **Action:** `create`
- **Capability:** Create Academic Program.

#### `academic_structure.department.create`
- **Action:** `create`
- **Capability:** Create Department.

#### `academic_structure.class.create`
- **Action:** `create`
- **Capability:** Create Class.

#### `academic_structure.class_program.assign`
- **Action:** `assign`
- **Capability:** Assign Class to Program.

#### `academic_structure.teacher_class.assign`
- **Action:** `assign`
- **Capability:** Assign Teacher to Class.

#### `academic_structure.term_structure.define`
- **Action:** `define`
- **Capability:** Define Term Structure.

#### `academic_structure.academic_calendar.update`
- **Action:** `update`
- **Capability:** Update Academic Calendar.

#### `academic_structure.academic_structure.archive`
- **Action:** `archive`
- **Capability:** Archive Academic Structure.

#### `academic_structure.academic_structures.clone`
- **Action:** `clone`
- **Capability:** Clone Academic Structures.

#### `academic_structure.structure_dependencies.validate`
- **Action:** `validate`
- **Capability:** Validate Structure Dependencies.

#### `academic_structure.academic_configuration.lock`
- **Action:** `lock`
- **Capability:** Lock Academic Configuration.

#### `academic_structure.academic_year.rollover`
- **Action:** `rollover`
- **Capability:** Roll Over Academic Year.

#### `academic_structure.structural_changes.track`
- **Action:** `track`
- **Capability:** Track Structural Changes.

#### `academic_structure.academic_setup.export`
- **Action:** `export`
- **Capability:** Export Academic Setup.

#### `academic_structure.academic_modifications.audit`
- **Action:** `audit`
- **Capability:** Audit Academic Modifications.

---

## TIER 3 — ACADEMIC OPERATIONS (CONTINUED)

### Attendance Management
_Namespace: `attendance`_

#### `attendance.attendance_rules.configure`
- **Action:** `configure`
- **Capability:** Configure Attendance Rules.

#### `attendance.attendance_types.define`
- **Action:** `define`
- **Capability:** Define Attendance Types.

#### `attendance.class_attendance_roster.load`
- **Action:** `load`
- **Capability:** Load Class Attendance Roster.

#### `attendance.student_attendance.record`
- **Action:** `record`
- **Capability:** Record Student Attendance.

#### `attendance.attendance_records.edit`
- **Action:** `edit`
- **Capability:** Edit Attendance Records.

#### `attendance.duplicate_attendance_entries.prevent`
- **Action:** `prevent`
- **Capability:** Prevent Duplicate Attendance Entries.

#### `attendance.unrecorded_attendance.auto_mark`
- **Action:** `auto_mark`
- **Capability:** Auto-Mark Unrecorded Attendance.

#### `attendance.attendance_student_profile.sync`
- **Action:** `sync`
- **Capability:** Sync Attendance to Student Profile.

#### `attendance.parents_absence.notify`
- **Action:** `notify`
- **Capability:** Notify Parents of Absence.

#### `attendance.attendance_summary.generate`
- **Action:** `generate`
- **Capability:** Generate Attendance Summary.

#### `attendance.attendance_anomalies.detect`
- **Action:** `detect`
- **Capability:** Detect Attendance Anomalies.

#### `attendance.attendance_records.lock`
- **Action:** `lock`
- **Capability:** Lock Attendance Records.

#### `attendance.locked_attendance.reopen`
- **Action:** `reopen`
- **Capability:** Reopen Locked Attendance.

#### `attendance.attendance_history.track`
- **Action:** `track`
- **Capability:** Track Attendance History.

#### `attendance.attendance_reports.export`
- **Action:** `export`
- **Capability:** Export Attendance Reports.

#### `attendance.role_based_attendance_access.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Role-Based Attendance Access.

#### `attendance.attendance_changes.audit`
- **Action:** `audit`
- **Capability:** Audit Attendance Changes.

#### `attendance.bulk_attendance_updates.support`
- **Action:** `support`
- **Capability:** Support Bulk Attendance Updates.

### Gradebook & Assessments (Future Project)
_Namespace: `gradebook`_

#### `gradebook.assessment_type.create`
- **Action:** `create`
- **Capability:** Create Assessment Type in the Gradebook & Assessments module.

#### `gradebook.grading_scheme.configure`
- **Action:** `configure`
- **Capability:** Configure Grading Scheme in the Gradebook & Assessments module.

#### `gradebook.assessments_classes.assign`
- **Action:** `assign`
- **Capability:** Assign Assessments to Classes in the Gradebook & Assessments module.

#### `gradebook.student_scores.enter`
- **Action:** `enter`
- **Capability:** Enter Student Scores in the Gradebook & Assessments module.

#### `gradebook.submitted_scores.edit`
- **Action:** `edit`
- **Capability:** Edit Submitted Scores in the Gradebook & Assessments module.

#### `gradebook.totals.calculate`
- **Action:** `calculate`
- **Capability:** Auto-Calculate Totals in the Gradebook & Assessments module.

#### `gradebook.weighted_grading_rules.apply`
- **Action:** `apply`
- **Capability:** Apply Weighted Grading Rules in the Gradebook & Assessments module.

#### `gradebook.score_ranges.validate`
- **Action:** `validate`
- **Capability:** Validate Score Ranges in the Gradebook & Assessments module.

#### `gradebook.gradebook.lock`
- **Action:** `lock`
- **Capability:** Lock Gradebook in the Gradebook & Assessments module.

#### `gradebook.gradebook.unlock`
- **Action:** `unlock`
- **Capability:** Unlock Gradebook in the Gradebook & Assessments module.

#### `gradebook.report_cards.generate`
- **Action:** `generate`
- **Capability:** Generate Report Cards in the Gradebook & Assessments module.

#### `gradebook.term_results.approve`
- **Action:** `approve`
- **Capability:** Approve Term Results in the Gradebook & Assessments module.

#### `gradebook.results_students.publish`
- **Action:** `publish`
- **Capability:** Publish Results to Students in the Gradebook & Assessments module.

#### `gradebook.parents_results.notify`
- **Action:** `notify`
- **Capability:** Notify Parents of Results in the Gradebook & Assessments module.

#### `gradebook.grade_history.track`
- **Action:** `track`
- **Capability:** Track Grade History in the Gradebook & Assessments module.

#### `gradebook.missing_grades.detect`
- **Action:** `detect`
- **Capability:** Detect Missing Grades in the Gradebook & Assessments module.

#### `gradebook.grade_reports.export`
- **Action:** `export`
- **Capability:** Export Grade Reports in the Gradebook & Assessments module.

#### `gradebook.continuous_assessment.support`
- **Action:** `support`
- **Capability:** Support Continuous Assessment in the Gradebook & Assessments module.

#### `gradebook.exam_assessments.support`
- **Action:** `support`
- **Capability:** Support Exam Assessments in the Gradebook & Assessments module.

#### `gradebook.grade_modifications.audit`
- **Action:** `audit`
- **Capability:** Audit Grade Modifications in the Gradebook & Assessments module.

### Academic Calendar & Timetables
_Namespace: `academic_calendar`_

#### `academic_calendar.academic_session.create`
- **Action:** `create`
- **Capability:** Create Academic Session.

#### `academic_calendar.academic_terms.define`
- **Action:** `define`
- **Capability:** Define Academic Terms.

#### `academic_calendar.school_calendar.configure`
- **Action:** `configure`
- **Capability:** Configure School Calendar.

#### `academic_calendar.holidays_and_breaks.set`
- **Action:** `set`
- **Capability:** Set Holidays & Breaks.

#### `academic_calendar.class_timetable.create`
- **Action:** `create`
- **Capability:** Create Class Timetable.

#### `academic_calendar.subjects_time_slots.assign`
- **Action:** `assign`
- **Capability:** Assign Subjects to Time Slots.

#### `academic_calendar.teachers_timetable.assign`
- **Action:** `assign`
- **Capability:** Assign Teachers to Timetable.

#### `academic_calendar.scheduling_conflicts.prevent`
- **Action:** `prevent`
- **Capability:** Prevent Scheduling Conflicts.

#### `academic_calendar.timetable.publish`
- **Action:** `publish`
- **Capability:** Publish Timetable.

#### `academic_calendar.timetable.update`
- **Action:** `update`
- **Capability:** Update Timetable.

#### `academic_calendar.users_schedule_changes.notify`
- **Action:** `notify`
- **Capability:** Notify Users of Schedule Changes.

#### `academic_calendar.academic_calendar.lock`
- **Action:** `lock`
- **Capability:** Lock Academic Calendar.

#### `academic_calendar.academic_calendar.clone`
- **Action:** `clone`
- **Capability:** Clone Academic Calendar.

#### `academic_calendar.calendar_new_year.rollover`
- **Action:** `rollover`
- **Capability:** Roll Over Calendar to New Year.

#### `academic_calendar.daily_schedule.view`
- **Action:** `view`
- **Capability:** Display Daily Schedule.

#### `academic_calendar.calendar_attendance.sync`
- **Action:** `sync`
- **Capability:** Sync Calendar with Attendance.

#### `academic_calendar.timetable.export`
- **Action:** `export`
- **Capability:** Export Timetable.

#### `academic_calendar.calendar_changes.track`
- **Action:** `track`
- **Capability:** Track Calendar Changes.

#### `academic_calendar.role_based_editing.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Role-Based Editing.

#### `academic_calendar.calendar_updates.audit`
- **Action:** `audit`
- **Capability:** Audit Calendar Updates.

---

## TIER 4 — FINANCIAL OPERATIONS

### Billing & Fees Management
_Namespace: `finance.billing`_

#### `finance.billing.fee_item.create`
- **Action:** `create`
- **Capability:** Create Fee Item.

#### `finance.billing.fee_structure.configure`
- **Action:** `configure`
- **Capability:** Configure Fee Structure.

#### `finance.billing.fees_classes.assign`
- **Action:** `assign`
- **Capability:** Assign Fees to Classes.

#### `finance.billing.fees_students.assign`
- **Action:** `assign`
- **Capability:** Assign Fees to Students.

#### `finance.billing.student_invoices.generate`
- **Action:** `generate`
- **Capability:** Generate Student Invoices.

#### `finance.billing.bulk_invoices.generate`
- **Action:** `generate`
- **Capability:** Generate Bulk Invoices.

#### `finance.billing.invoice_items.edit`
- **Action:** `edit`
- **Capability:** Edit Invoice Items.

#### `finance.billing.late_fee_rules.apply`
- **Action:** `apply`
- **Capability:** Apply Late Fee Rules.

#### `finance.billing.discounts.apply`
- **Action:** `apply`
- **Capability:** Apply Discounts.

#### `finance.billing.scholarships.apply`
- **Action:** `apply`
- **Capability:** Apply Scholarships.

#### `finance.billing.payment_deadlines.configure`
- **Action:** `configure`
- **Capability:** Configure Payment Deadlines.

#### `finance.billing.invoice_status.track`
- **Action:** `track`
- **Capability:** Track Invoice Status.

#### `finance.billing.issued_invoices.lock`
- **Action:** `lock`
- **Capability:** Lock Issued Invoices.

#### `finance.billing.invoices.cancel`
- **Action:** `cancel`
- **Capability:** Cancel Invoices.

#### `finance.billing.invoices.reissue`
- **Action:** `reissue`
- **Capability:** Reissue Invoices.

#### `finance.billing.payers_invoices.notify`
- **Action:** `notify`
- **Capability:** Notify Payers of Invoices.

#### `finance.billing.fee_history.track`
- **Action:** `track`
- **Capability:** Track Fee History.

#### `finance.billing.billing_reports.export`
- **Action:** `export`
- **Capability:** Export Billing Reports.

#### `finance.billing.billing_permissions.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Billing Permissions.

#### `finance.billing.billing_actions.audit`
- **Action:** `audit`
- **Capability:** Audit Billing Actions.

### Payments & Reconciliation
_Namespace: `finance.payment`_

#### `finance.payment.payment_gateways.configure`
- **Action:** `configure`
- **Capability:** Configure Payment Gateways.

#### `finance.payment.payment_channels.enable`
- **Action:** `enable`
- **Capability:** Enable Payment Channels.

#### `finance.payment.online_payments.process`
- **Action:** `process`
- **Capability:** Process Online Payments.

#### `finance.payment.offline_payments.record`
- **Action:** `record`
- **Capability:** Record Offline Payments.

#### `finance.payment.payment_receipts.generate`
- **Action:** `generate`
- **Capability:** Generate Payment Receipts.

#### `finance.payment.payments_invoices.match`
- **Action:** `match`
- **Capability:** Match Payments to Invoices.

#### `finance.payment.partial_payments.handle`
- **Action:** `handle`
- **Capability:** Handle Partial Payments.

#### `finance.payment.overpayments.handle`
- **Action:** `handle`
- **Capability:** Handle Overpayments.

#### `finance.payment.failed_payments.detect`
- **Action:** `detect`
- **Capability:** Detect Failed Payments.

#### `finance.payment.failed_payments.retry`
- **Action:** `retry`
- **Capability:** Retry Failed Payments.

#### `finance.payment.gateway_transactions.reconcile`
- **Action:** `reconcile`
- **Capability:** Reconcile Gateway Transactions.

#### `finance.payment.bank_transfers.reconcile`
- **Action:** `reconcile`
- **Capability:** Reconcile Bank Transfers.

#### `finance.payment.refund_requests.process`
- **Action:** `process`
- **Capability:** Process Refund Requests.

#### `finance.payment.refunds.approve`
- **Action:** `approve`
- **Capability:** Approve Refunds.

#### `finance.payment.refunds.execute`
- **Action:** `execute`
- **Capability:** Execute Refunds.

#### `finance.payment.payment_status.track`
- **Action:** `track`
- **Capability:** Track Payment Status.

#### `finance.payment.users_payment_events.notify`
- **Action:** `notify`
- **Capability:** Notify Users of Payment Events.

#### `finance.payment.payment_history.export`
- **Action:** `export`
- **Capability:** Export Payment History.

#### `finance.payment.payment_approvals.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Payment Approvals.

#### `finance.payment.payment_transactions.audit`
- **Action:** `audit`
- **Capability:** Audit Payment Transactions.

### Finance Ledger & Reporting
_Namespace: `finance.ledger`_

#### `finance.ledger.chart_accounts.configure`
- **Action:** `configure`
- **Capability:** Configure Chart of Accounts.

#### `finance.ledger.ledger_accounts.create`
- **Action:** `create`
- **Capability:** Create Ledger Accounts.

#### `finance.ledger.financial_transactions.record`
- **Action:** `record`
- **Capability:** Record Financial Transactions.

#### `finance.ledger.billing_entries.post`
- **Action:** `post`
- **Capability:** Auto-Post Billing Entries.

#### `finance.ledger.payment_entries.post`
- **Action:** `post`
- **Capability:** Auto-Post Payment Entries.

#### `finance.ledger.expense_entries.record`
- **Action:** `record`
- **Capability:** Record Expense Entries.

#### `finance.ledger.trial_balance.generate`
- **Action:** `generate`
- **Capability:** Generate Trial Balance.

#### `finance.ledger.income_statement.generate`
- **Action:** `generate`
- **Capability:** Generate Income Statement.

#### `finance.ledger.balance_sheet.generate`
- **Action:** `generate`
- **Capability:** Generate Balance Sheet.

#### `finance.ledger.cash_flow_report.generate`
- **Action:** `generate`
- **Capability:** Generate Cash Flow Report.

#### `finance.ledger.outstanding_balances.track`
- **Action:** `track`
- **Capability:** Track Outstanding Balances.

#### `finance.ledger.financial_reports.filter`
- **Action:** `filter`
- **Capability:** Filter Financial Reports.

#### `finance.ledger.financial_statements.export`
- **Action:** `export`
- **Capability:** Export Financial Statements.

#### `finance.ledger.financial_periods.lock`
- **Action:** `lock`
- **Capability:** Lock Financial Periods.

#### `finance.ledger.financial_periods.reopen`
- **Action:** `reopen`
- **Capability:** Reopen Financial Periods.

#### `finance.ledger.ledger_adjustments.track`
- **Action:** `track`
- **Capability:** Track Ledger Adjustments.

#### `finance.ledger.ledger_integrity.validate`
- **Action:** `validate`
- **Capability:** Validate Ledger Integrity.

#### `finance.ledger.approval_controls.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Approval Controls.

#### `finance.ledger.financial_records.audit`
- **Action:** `audit`
- **Capability:** Audit Financial Records.

#### `finance.ledger.financial_reports.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Financial Reports.

### Discounts, Refunds & Adjustments (Future Project)
_Namespace: `finance.adjustment`_

#### `finance.adjustment.discount_policy.create`
- **Action:** `create`
- **Capability:** Create Discount Policy in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.discounts_students.assign`
- **Action:** `assign`
- **Capability:** Assign Discounts to Students in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.bulk_discounts.apply`
- **Action:** `apply`
- **Capability:** Apply Bulk Discounts in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.discount_eligibility.validate`
- **Action:** `validate`
- **Capability:** Validate Discount Eligibility in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.discount_requests.approve`
- **Action:** `approve`
- **Capability:** Approve Discount Requests in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.discounts.revoke`
- **Action:** `revoke`
- **Capability:** Revoke Discounts in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.refund_request.initiate`
- **Action:** `initiate`
- **Capability:** Initiate Refund Request in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.refund_amount.validate`
- **Action:** `validate`
- **Capability:** Validate Refund Amount in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.refund_workflow.approve`
- **Action:** `approve`
- **Capability:** Approve Refund Workflow in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.refund_payment.execute`
- **Action:** `execute`
- **Capability:** Execute Refund Payment in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.billing_adjustments.apply`
- **Action:** `apply`
- **Capability:** Apply Billing Adjustments in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.financial_entries.reverse`
- **Action:** `reverse`
- **Capability:** Reverse Financial Entries in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.adjustment_history.track`
- **Action:** `track`
- **Capability:** Track Adjustment History in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.stakeholders_adjustments.notify`
- **Action:** `notify`
- **Capability:** Notify Stakeholders of Adjustments in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.adjustment_limits.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Adjustment Limits in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.adjustments_post_approval.lock`
- **Action:** `lock`
- **Capability:** Lock Adjustments Post-Approval in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.duplicate_refunds.prevent`
- **Action:** `prevent`
- **Capability:** Prevent Duplicate Refunds in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.adjustment_reports.export`
- **Action:** `export`
- **Capability:** Export Adjustment Reports in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.discount_actions.audit`
- **Action:** `audit`
- **Capability:** Audit Discount Actions in the Discounts, Refunds & Adjustments module.

#### `finance.adjustment.refund_activities.audit`
- **Action:** `audit`
- **Capability:** Audit Refund Activities in the Discounts, Refunds & Adjustments module.

---

## TIER 5 — PROCUREMENT & ASSETS

### Vendor Management (Future Project)
_Namespace: `procurement.vendor`_

#### `procurement.vendor.vendor.register`
- **Action:** `register`
- **Capability:** Register Vendor in the Vendor Management module.

#### `procurement.vendor.vendor_registration.approve`
- **Action:** `approve`
- **Capability:** Approve Vendor Registration in the Vendor Management module.

#### `procurement.vendor.vendors.categorize`
- **Action:** `categorize`
- **Capability:** Categorize Vendors in the Vendor Management module.

#### `procurement.vendor.vendor_profile.update`
- **Action:** `update`
- **Capability:** Update Vendor Profile in the Vendor Management module.

#### `procurement.vendor.vendor.deactivate`
- **Action:** `deactivate`
- **Capability:** Deactivate Vendor in the Vendor Management module.

#### `procurement.vendor.vendor_performance.rate`
- **Action:** `rate`
- **Capability:** Rate Vendor Performance in the Vendor Management module.

#### `procurement.vendor.vendor_history.track`
- **Action:** `track`
- **Capability:** Track Vendor History in the Vendor Management module.

#### `procurement.vendor.vendor_contracts.assign`
- **Action:** `assign`
- **Capability:** Assign Vendor Contracts in the Vendor Management module.

#### `procurement.vendor.vendor_documents.upload`
- **Action:** `upload`
- **Capability:** Upload Vendor Documents in the Vendor Management module.

#### `procurement.vendor.vendor_compliance.validate`
- **Action:** `validate`
- **Capability:** Validate Vendor Compliance in the Vendor Management module.

#### `procurement.vendor.vendor_directory.search`
- **Action:** `search`
- **Capability:** Search Vendor Directory in the Vendor Management module.

#### `procurement.vendor.vendor_list.export`
- **Action:** `export`
- **Capability:** Export Vendor List in the Vendor Management module.

#### `procurement.vendor.duplicate_vendors.detect`
- **Action:** `detect`
- **Capability:** Detect Duplicate Vendors in the Vendor Management module.

#### `procurement.vendor.vendor_records.lock`
- **Action:** `lock`
- **Capability:** Lock Vendor Records in the Vendor Management module.

#### `procurement.vendor.vendor_account.restore`
- **Action:** `restore`
- **Capability:** Restore Vendor Account in the Vendor Management module.

#### `procurement.vendor.vendor_spend_summary.view`
- **Action:** `view`
- **Capability:** View Vendor Spend Summary in the Vendor Management module.

#### `procurement.vendor.vendor_payment_status.track`
- **Action:** `track`
- **Capability:** Track Vendor Payment Status in the Vendor Management module.

#### `procurement.vendor.vendor_changes.audit`
- **Action:** `audit`
- **Capability:** Audit Vendor Changes in the Vendor Management module.

### Procurement Requests & Approvals
_Namespace: `procurement.request`_

#### `procurement.request.purchase_request.create`
- **Action:** `create`
- **Capability:** Create Purchase Request.

#### `procurement.request.purchase_request.edit`
- **Action:** `edit`
- **Capability:** Edit Purchase Request.

#### `procurement.request.budget_availability.validate`
- **Action:** `validate`
- **Capability:** Validate Budget Availability.

#### `procurement.request.request_approval.submit`
- **Action:** `submit`
- **Capability:** Submit Request for Approval.

#### `procurement.request.approval_workflow.route`
- **Action:** `route`
- **Capability:** Route Approval Workflow.

#### `procurement.request.purchase_request.approve`
- **Action:** `approve`
- **Capability:** Approve Purchase Request.

#### `procurement.request.purchase_request.reject`
- **Action:** `reject`
- **Capability:** Reject Purchase Request.

#### `procurement.request.high_value_requests.escalate`
- **Action:** `escalate`
- **Capability:** Escalate High-Value Requests.

#### `procurement.request.approval_status.track`
- **Action:** `track`
- **Capability:** Track Approval Status.

#### `procurement.request.approval_comments.add`
- **Action:** `add`
- **Capability:** Add Approval Comments.

#### `procurement.request.spending_limits.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Spending Limits.

#### `procurement.request.purchase_request.cancel`
- **Action:** `cancel`
- **Capability:** Cancel Purchase Request.

#### `procurement.request.request_stakeholders.notify`
- **Action:** `notify`
- **Capability:** Notify Request Stakeholders.

#### `procurement.request.request_po.convert`
- **Action:** `convert`
- **Capability:** Convert Request to PO.

#### `procurement.request.request_history.track`
- **Action:** `track`
- **Capability:** Track Request History.

#### `procurement.request.request_data.export`
- **Action:** `export`
- **Capability:** Export Request Data.

#### `procurement.request.role_based_approvals.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Role-Based Approvals.

#### `procurement.request.request_actions.audit`
- **Action:** `audit`
- **Capability:** Audit Request Actions.

### Purchase Orders & Delivery
_Namespace: `procurement.purchase_order`_

#### `procurement.purchase_order.purchase_order.generate`
- **Action:** `generate`
- **Capability:** Generate Purchase Order.

#### `procurement.purchase_order.vendor_po.assign`
- **Action:** `assign`
- **Capability:** Assign Vendor to PO.

#### `procurement.purchase_order.po_vendor.send`
- **Action:** `send`
- **Capability:** Send PO to Vendor.

#### `procurement.purchase_order.po_status.update`
- **Action:** `update`
- **Capability:** Update PO Status.

#### `procurement.purchase_order.po_receipt.acknowledge`
- **Action:** `acknowledge`
- **Capability:** Acknowledge PO Receipt.

#### `procurement.purchase_order.delivery_timeline.track`
- **Action:** `track`
- **Capability:** Track Delivery Timeline.

#### `procurement.purchase_order.goods_received.record`
- **Action:** `record`
- **Capability:** Record Goods Received.

#### `procurement.purchase_order.delivered_quantity.validate`
- **Action:** `validate`
- **Capability:** Validate Delivered Quantity.

#### `procurement.purchase_order.delivered_quality.validate`
- **Action:** `validate`
- **Capability:** Validate Delivered Quality.

#### `procurement.purchase_order.delivered_items.reject`
- **Action:** `reject`
- **Capability:** Reject Delivered Items.

#### `procurement.purchase_order.delivery_completion.approve`
- **Action:** `approve`
- **Capability:** Approve Delivery Completion.

#### `procurement.purchase_order.invoice_matching.trigger`
- **Action:** `trigger`
- **Capability:** Trigger Invoice Matching.

#### `procurement.purchase_order.purchase_order.close`
- **Action:** `close`
- **Capability:** Close Purchase Order.

#### `procurement.purchase_order.purchase_order.cancel`
- **Action:** `cancel`
- **Capability:** Cancel Purchase Order.

#### `procurement.purchase_order.po_documents.export`
- **Action:** `export`
- **Capability:** Export PO Documents.

#### `procurement.purchase_order.po_history.track`
- **Action:** `track`
- **Capability:** Track PO History.

#### `procurement.purchase_order.delivery_stakeholders.notify`
- **Action:** `notify`
- **Capability:** Notify Delivery Stakeholders.

#### `procurement.purchase_order.po_changes.audit`
- **Action:** `audit`
- **Capability:** Audit PO Changes.

### Inventory & Asset Tracking
_Namespace: `inventory`_

#### `inventory.inventory_item.register`
- **Action:** `register`
- **Capability:** Register Inventory Item.

#### `inventory.inventory_category.assign`
- **Action:** `assign`
- **Capability:** Assign Inventory Category.

#### `inventory.inventory_quantity.track`
- **Action:** `track`
- **Capability:** Track Inventory Quantity.

#### `inventory.stock_levels.update`
- **Action:** `update`
- **Capability:** Update Stock Levels.

#### `inventory.reorder_thresholds.set`
- **Action:** `set`
- **Capability:** Set Reorder Thresholds.

#### `inventory.stock_alerts.generate`
- **Action:** `generate`
- **Capability:** Generate Stock Alerts.

#### `inventory.asset_acquisition.record`
- **Action:** `record`
- **Capability:** Record Asset Acquisition.

#### `inventory.asset_location.assign`
- **Action:** `assign`
- **Capability:** Assign Asset Location.

#### `inventory.asset_depreciation.track`
- **Action:** `track`
- **Capability:** Track Asset Depreciation.

#### `inventory.asset_disposal.record`
- **Action:** `record`
- **Capability:** Record Asset Disposal.

#### `inventory.inventory_movements.audit`
- **Action:** `audit`
- **Capability:** Audit Inventory Movements.

#### `inventory.stock_reconciliation.perform`
- **Action:** `perform`
- **Capability:** Perform Stock Reconciliation.

#### `inventory.inventory_records.lock`
- **Action:** `lock`
- **Capability:** Lock Inventory Records.

#### `inventory.inventory_reports.export`
- **Action:** `export`
- **Capability:** Export Inventory Reports.

#### `inventory.asset_history.track`
- **Action:** `track`
- **Capability:** Track Asset History.

#### `inventory.assets_departments.assign`
- **Action:** `assign`
- **Capability:** Assign Assets to Departments.

#### `inventory.inventory_permissions.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Inventory Permissions.

#### `inventory.asset_records.archive`
- **Action:** `archive`
- **Capability:** Archive Asset Records.

---

## TIER 6 — COMMUNICATION & ENGAGEMENT

### Messaging & Notifications
_Namespace: `communication`_

#### `communication.internal_message.send`
- **Action:** `send`
- **Capability:** Send Internal Message.

#### `communication.bulk_notifications.send`
- **Action:** `send`
- **Capability:** Send Bulk Notifications.

#### `communication.email_notifications.send`
- **Action:** `send`
- **Capability:** Send Email Notifications.

#### `communication.sms_notifications.send`
- **Action:** `send`
- **Capability:** Send SMS Notifications.

#### `communication.notification_templates.configure`
- **Action:** `configure`
- **Capability:** Configure Notification Templates.

#### `communication.files_messages.attach`
- **Action:** `attach`
- **Capability:** Attach Files to Messages.

#### `communication.message_delivery.track`
- **Action:** `track`
- **Capability:** Track Message Delivery.

#### `communication.message_history.view`
- **Action:** `view`
- **Capability:** View Message History.

#### `communication.messages_by_type.filter`
- **Action:** `filter`
- **Capability:** Filter Messages by Type.

#### `communication.messages.reply`
- **Action:** `reply`
- **Capability:** Reply to Messages.

#### `communication.notification_threads.mute`
- **Action:** `mute`
- **Capability:** Mute Notification Threads.

#### `communication.notifications.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Notifications.

#### `communication.communication_permissions.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Communication Permissions.

#### `communication.communication_events.log`
- **Action:** `log`
- **Capability:** Log Communication Events.

#### `communication.message_activity.audit`
- **Action:** `audit`
- **Capability:** Audit Message Activity.

### Student Portal
_Namespace: `student_portal`_

#### `student_portal.student_login.create`
- **Action:** `create`
- **Capability:** Create Student Login.

#### `student_portal.class_timetable.view`
- **Action:** `view`
- **Capability:** View Class Timetable.

#### `student_portal.attendance_summary.view`
- **Action:** `view`
- **Capability:** View Attendance Summary.

#### `student_portal.academic_results.view`
- **Action:** `view`
- **Capability:** View Academic Results.

#### `student_portal.learning_materials.access`
- **Action:** `access`
- **Capability:** Access Learning Materials.

#### `student_portal.announcements.receive`
- **Action:** `receive`
- **Capability:** Receive Announcements.

#### `student_portal.fee_status.view`
- **Action:** `view`
- **Capability:** View Fee Status.

#### `student_portal.receipts.download`
- **Action:** `download`
- **Capability:** Download Receipts.

#### `student_portal.teachers.message`
- **Action:** `message`
- **Capability:** Message Teachers.

#### `student_portal.profile_details.update`
- **Action:** `update`
- **Capability:** Update Profile Details.

#### `student_portal.academic_progress.track`
- **Action:** `track`
- **Capability:** Track Academic Progress.

#### `student_portal.student_permissions.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Student Permissions.

#### `student_portal.student_activity.audit`
- **Action:** `audit`
- **Capability:** Audit Student Activity.

---

## TIER 7 — REPORTING & ANALYTICS

### Dashboards & KPIs
_Namespace: `analytics.dashboard`_

#### `analytics.dashboard.institution_overview_dashboard.view`
- **Action:** `view`
- **Capability:** Display Institution Overview Dashboard.

#### `analytics.dashboard.academic_performance_kpis.view`
- **Action:** `view`
- **Capability:** Display Academic Performance KPIs.

#### `analytics.dashboard.attendance_kpis.view`
- **Action:** `view`
- **Capability:** Display Attendance KPIs.

#### `analytics.dashboard.financial_kpis.view`
- **Action:** `view`
- **Capability:** Display Financial KPIs.

#### `analytics.dashboard.procurement_kpis.view`
- **Action:** `view`
- **Capability:** Display Procurement KPIs.

#### `analytics.dashboard.dashboard_metrics.filter`
- **Action:** `filter`
- **Capability:** Filter Dashboard Metrics.

#### `analytics.dashboard.kpis.drilldown`
- **Action:** `drilldown`
- **Capability:** Drill Down into KPIs.

#### `analytics.dashboard.period_performance.compare`
- **Action:** `compare`
- **Capability:** Compare Period Performance.

#### `analytics.dashboard.dashboard_widgets.configure`
- **Action:** `configure`
- **Capability:** Configure Dashboard Widgets.

#### `analytics.dashboard.custom_dashboards.save`
- **Action:** `save`
- **Capability:** Save Custom Dashboards.

#### `analytics.dashboard.dashboard_access_control.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Dashboard Access Control.

#### `analytics.dashboard.dashboard_data.refresh`
- **Action:** `refresh`
- **Capability:** Refresh Dashboard Data.

#### `analytics.dashboard.dashboard_views.export`
- **Action:** `export`
- **Capability:** Export Dashboard Views.

#### `analytics.dashboard.dashboard_usage.track`
- **Action:** `track`
- **Capability:** Track Dashboard Usage.

#### `analytics.dashboard.dashboard_changes.audit`
- **Action:** `audit`
- **Capability:** Audit Dashboard Changes.

### Operational Reports
_Namespace: `reporting.operational`_

#### `reporting.operational.attendance_reports.generate`
- **Action:** `generate`
- **Capability:** Generate Attendance Reports.

#### `reporting.operational.academic_reports.generate`
- **Action:** `generate`
- **Capability:** Generate Academic Reports.

#### `reporting.operational.financial_reports.generate`
- **Action:** `generate`
- **Capability:** Generate Financial Reports.

#### `reporting.operational.procurement_reports.generate`
- **Action:** `generate`
- **Capability:** Generate Procurement Reports.

#### `reporting.operational.user_activity_reports.generate`
- **Action:** `generate`
- **Capability:** Generate User Activity Reports.

#### `reporting.operational.report_generation.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Report Generation.

#### `reporting.operational.report_parameters.filter`
- **Action:** `filter`
- **Capability:** Filter Report Parameters.

#### `reporting.operational.reports.preview`
- **Action:** `preview`
- **Capability:** Preview Reports.

#### `reporting.operational.reports_pdf.export`
- **Action:** `export`
- **Capability:** Export Reports to PDF.

#### `reporting.operational.reports_excel.export`
- **Action:** `export`
- **Capability:** Export Reports to Excel.

#### `reporting.operational.reports_email.share`
- **Action:** `share`
- **Capability:** Share Reports via Email.

#### `reporting.operational.generated_reports.archive`
- **Action:** `archive`
- **Capability:** Archive Generated Reports.

#### `reporting.operational.report_history.track`
- **Action:** `track`
- **Capability:** Track Report History.

#### `reporting.operational.report_access_control.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Report Access Control.

#### `reporting.operational.report_generation.audit`
- **Action:** `audit`
- **Capability:** Audit Report Generation.

### Export & Data Access
_Namespace: `data_export`_

#### `data_export.student_data.export`
- **Action:** `export`
- **Capability:** Export Student Data.

#### `data_export.staff_data.export`
- **Action:** `export`
- **Capability:** Export Staff Data.

#### `data_export.attendance_data.export`
- **Action:** `export`
- **Capability:** Export Attendance Data.

#### `data_export.academic_results.export`
- **Action:** `export`
- **Capability:** Export Academic Results.

#### `data_export.financial_records.export`
- **Action:** `export`
- **Capability:** Export Financial Records.

#### `data_export.procurement_records.export`
- **Action:** `export`
- **Capability:** Export Procurement Records.

#### `data_export.audit_logs.export`
- **Action:** `export`
- **Capability:** Export Audit Logs.

#### `data_export.export_formats.configure`
- **Action:** `configure`
- **Capability:** Configure Export Formats.

#### `data_export.data_access_permissions.enforce`
- **Action:** `enforce`
- **Capability:** Enforce Data Access Permissions.

#### `data_export.data_masking_rules.apply`
- **Action:** `apply`
- **Capability:** Apply Data Masking Rules.

#### `data_export.automated_exports.schedule`
- **Action:** `schedule`
- **Capability:** Schedule Automated Exports.

#### `data_export.secure_download_links.generate`
- **Action:** `generate`
- **Capability:** Generate Secure Download Links.

#### `data_export.export_requests.track`
- **Action:** `track`
- **Capability:** Track Export Requests.

#### `data_export.export_files.expire`
- **Action:** `expire`
- **Capability:** Expire Export Files.

#### `data_export.data_access_events.audit`
- **Action:** `audit`
- **Capability:** Audit Data Access Events.

---
