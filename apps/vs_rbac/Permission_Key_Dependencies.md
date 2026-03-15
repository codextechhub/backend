# CodeX Vision Permission Dependency Registry

Recommended dependency map for the permission keys in `CodeX_Vision_Permission_Key_Registry.md`.

This file shows **which key depends on which other key** so teammates can understand prerequisite access, compose role templates safely, and avoid granting dangerous actions without their base guards.

> Important: these are **recommended implementation dependencies inferred from the permission catalog**, not hard-coded truths from the PRD. They are meant for RBAC design, seeding, code review, and policy discussions.

## Dependency rules used

- Business-domain permissions generally depend on authenticated API access plus valid tenant context.
- Read/export/search-style permissions usually depend on a matching `*.view` permission where one exists.
- Create/update/delete/approve/state-change permissions usually depend on both base access and audit logging.
- Export/download/share flows may also depend on masking, secure-link, and expiry controls.

## Global Platform Guards

### Global Platform Guards

#### `system.session.access.authenticate`
- **Action:** `access`
- **Depends on:** none
- **Why:** standalone or top-level permission with no direct prerequisite in this catalog.

#### `system.authenticated.access`
- **Action:** `access`
- **Depends on:** none
- **Why:** Root authentication gate; other protected permissions usually depend on this..

#### `system.api.access`
- **Action:** `require`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.tenant_context.require`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.tenant_boundary.enforce`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.mfa.enforce`
- **Action:** `reset`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.mfa.reset`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `system.password.policy.enforce`
- **Action:** `refresh`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.token.refresh`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.rate_limit.login.enforce`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.session.view`
- **Action:** `force`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.session.force_logout`
- **Action:** `write`
- **Depends on:**
  - `system.authenticated.access`
  - `system.session.view`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `system.audit.write`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.audit.view`
- **Action:** `search`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.audit.search`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.view`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.audit.export`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.view`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.configuration.view`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.configuration.update`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.configuration.view`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system.feature_flag.view`
- **Action:** `toggle`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.feature_flag.toggle`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.feature_flag.view`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system.feature_flag.schedule`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.health.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.security_alert.view`
- **Action:** `impersonate`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system.support.impersonation.impersonate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.write`
  - `system.mfa.enforce`
- **Why:** mutation should be captured by audit logging.

#### `system.download.secure_link.generate`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `system.data_masking.apply`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system.export.schedule`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.export.expiry.enforce`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
- **Why:** foundational platform guards apply.

#### `system.role_request.approve`
- **Action:** `deny`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.write`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `system.role_request.deny`
- **Action:** `override`
- **Depends on:**
  - `system.authenticated.access`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `system.approval.override.super_admin`
- **Action:** `override`
- **Depends on:**
  - `system.authenticated.access`
  - `system.mfa.enforce`
- **Why:** foundational platform guards apply.

## TIER 1 — CORE PLATFORM MODULES

### Institution Management

#### `institution.institution.create`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `institution.institution_slug.generate`
- **Action:** `provision`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `institution.database_schema.provision`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `institution.localization.assign`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `institution.lifecycle.configure`
- **Action:** `toggle`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `institution.module_access.toggle`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `institution.primary_administrator.assign`
- **Action:** `upload`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `institution.branding_assets.upload`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `institution.metadata.update`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `institution.uniqueness.validate`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `institution.data_isolation.enforce`
- **Action:** `suspend`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `institution.access_state.suspend`
- **Action:** `reactivate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `institution.access_state.reactivate`
- **Action:** `soft_delete`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `institution.deletion_state.soft_delete`
- **Action:** `hard_delete`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `institution.deletion_state.hard_delete`
- **Action:** `reset`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `institution.configuration.reset`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `institution.health_status.view`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `institution.creation_audit_logs.track`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `institution.feature_flags.enforce`
- **Action:** `rollback`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `institution.provisioning.rollback`
- **Action:** `rollback`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

### Vision Admin Console (Internal Backoffice)

#### `vision_admin.session.authenticate`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `vision_admin.institution_dashboard.view`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `vision_admin.institution_configuration.edit`
- **Action:** `suspend_manage`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `vision_admin.access_state.suspend_manage`
- **Action:** `reset`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `vision_admin.data_configuration.reset`
- **Action:** `monitor`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `vision_admin.institution_provisioning_pipeline.monitor`
- **Action:** `retry`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `vision_admin.failed_provisioning_steps.retry`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `vision_admin.institution_usage_metrics.view`
- **Action:** `access`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `vision_admin.institution_audit_logs.access`
- **Action:** `impersonate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `vision_admin.institution_user.impersonate`
- **Action:** `manage`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.mfa.enforce`
- **Why:** mutation should be captured by audit logging.

#### `vision_admin.role_change_request.manage`
- **Action:** `review`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** change-style permission usually requires seeing the current state first.

#### `vision_admin.import_jobs.review`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** approval/review flow usually depends on an existing request or reviewable record.

#### `vision_admin.data_fix.apply`
- **Action:** `toggle`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `vision_admin.feature_flags.toggle`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `vision_admin.health_metrics.view`
- **Action:** `manage`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `vision_admin.admin_roles.manage`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** change-style permission usually requires seeing the current state first.

#### `vision_admin.mfa.enforce`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `vision_admin.security_alerts.view`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `vision_admin.admin_actions.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### User Identity, Accounts & Authentication

#### `identity.user_account.create`
- **Action:** `invite`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `identity.user_email.invite`
- **Action:** `activate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `identity.user_account.activate`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `identity.user_account.create`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `identity.password_policy.enforce`
- **Action:** `refresh`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `identity.access_token.refresh`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `identity.user_account.lock`
- **Action:** `unlock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `identity.user_account.create`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `identity.user_account.unlock`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `identity.user_account.create`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `identity.login_sessions.track`
- **Action:** `force`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `identity.user_logout.force`
- **Action:** `reset`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `identity.user_password.reset`
- **Action:** `verify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `identity.email_address.verify`
- **Action:** `verify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `identity.phone_number.verify`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `identity.institution_aware_login.enforce`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `identity.authentication_events.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Roles, Permissions & Access Control

#### `rbac.role_template.create`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `rbac.role_user.assign`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `rbac.role_permissions.update`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `rbac.role_based_access_control.enforce`
- **Action:** `restrict`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `rbac.cross_module_access.restrict`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `rbac.permission_dependencies.validate`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `rbac.permission_changes_instantly.apply`
- **Action:** `request`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `rbac.role_change.request`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `rbac.role_change.approve`
- **Action:** `deny`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `rbac.role_change.request`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `rbac.role_change_request.deny`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `rbac.multiple_roles_user.assign`
- **Action:** `deactivate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `rbac.role.deactivate`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `rbac.role_template.archive`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `rbac.role_template.create`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `rbac.api_level_permission_checks.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `rbac.role_changes.audit`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `rbac.compliance_constraints.enforce`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `rbac.permission_matrix.view`
- **Action:** `restore`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `rbac.previous_role_state.restore`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `rbac.critical_system_roles.lock`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

### Audit Logging & Compliance

#### `audit.user_creation_events.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.authentication_events.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.data_import_actions.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.configuration_changes.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.financial_transactions.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.procurement_actions.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.permission_changes.log`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.immutable_log_storage.enforce`
- **Action:** `timestamp`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `audit.audit_events.timestamp`
- **Action:** `attribute`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `audit.actions_actor.attribute`
- **Action:** `search`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.audit_logs.search`
- **Action:** `filter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.logs_by_action_type.filter`
- **Action:** `filter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.logs_by_date_range.filter`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.audit_logs.export`
- **Action:** `paginate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.log_retrieval.paginate`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `audit.audit_trail_entity.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### System Configuration & Feature Flags

#### `system_config.global_system_settings.view`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system_config.global_system_settings.update`
- **Action:** `create`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system_config.global_system_settings.view`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system_config.configuration_key.create`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `system_config.configuration_key.edit`
- **Action:** `delete`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system_config.configuration_key.delete`
- **Action:** `restore`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system_config.configuration_key.create`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `system_config.deleted_configuration_key.restore`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `system_config.configuration_schema.validate`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `system_config.configuration_access_permissions.enforce`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `system_config.configuration_change_history.track`
- **Action:** `rollback`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system_config.configuration_changes.rollback`
- **Action:** `create`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `system_config.feature_flag.create`
- **Action:** `enable`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `system_config.feature_flag_globally.enable`
- **Action:** `disable`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system_config.feature_flag_globally.disable`
- **Action:** `enable`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system_config.feature_flag_institution.enable`
- **Action:** `disable`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system_config.feature_flag_institution.disable`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `system_config.feature_flag_activation.schedule`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `system_config.feature_flag_deactivation.schedule`
- **Action:** `restrict`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `system_config.flags_specific_roles.restrict`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `system_config.feature_flag_changes.audit`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system_config.conflicting_flag_rules.detect`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `system_config.configuration_and_feature_flag_settings.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

## TIER 2 — ONBOARDING & DATA FOUNDATION

### Institution Onboarding Module

#### `onboarding.institution_onboarding.initiate`
- **Action:** `capture`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `onboarding.institution_metadata.capture`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `onboarding.academic_structure.configure`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `onboarding.financial_structure.configure`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `onboarding.procurement_structure.configure`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `onboarding.default_roles.assign`
- **Action:** `seed`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `onboarding.default_permissions.seed`
- **Action:** `upload`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `onboarding.initial_datasets.upload`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `onboarding.onboarding_completeness.validate`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `onboarding.onboarding_progress.track`
- **Action:** `block`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `onboarding.go_live_on_validation_errors.block`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** state-changing action depends on the target record already existing.

#### `onboarding.training_session.schedule`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `onboarding.onboarding_checklist.generate`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `onboarding.onboarding_actions.log`
- **Action:** `rollback`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `onboarding.incomplete_onboarding.rollback`
- **Action:** `monitor`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `onboarding.post_go_live_activity.monitor`
- **Action:** `escalate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `onboarding.onboarding_issues.escalate`
- **Action:** `escalate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** approval/review flow usually depends on an existing request or reviewable record.

### Data Import & Validation Engine

#### `data_import.csv_file.upload`
- **Action:** `upload`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `data_import.excel_file.upload`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `data_import.dataset_type.detect`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_import.file_structure.validate`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `data_import.mandatory_fields.validate`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `data_import.duplicate_records.detect`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_import.cross_entity_references.validate`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `data_import.auto_mapping_rules.apply`
- **Action:** `map`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `data_import.field_mapping.map`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `data_import.validation_error_report.generate`
- **Action:** `classify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `data_import.errors_vs_warnings.classify`
- **Action:** `block`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_import.import_on_critical_errors.block`
- **Action:** `execute`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** state-changing action depends on the target record already existing.

#### `data_import.background_import_job.execute`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `data_import.import_progress.track`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_import.error_import_rows.edit`
- **Action:** `rollback`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `data_import.failed_imports.rollback`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `data_import.data_changes.log`
- **Action:** `support`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_import.large_dataset_imports.support`
- **Action:** `store`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `data_import.import_history.store`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `data_import.admin_on_completion.notify`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

## TIER 3 — ACADEMIC OPERATIONS (FOUNDATION LIST)

### Student Management

#### `student.student_profile.create`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `student.student_records.update`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `student.student_class.assign`
- **Action:** `promote`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `student.student_between_terms_sessions.promote`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `student.student_records.archive`
- **Action:** `restore`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `student.student_records.update`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `student.archived_students.restore`
- **Action:** `manage`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `student.student_status.manage`
- **Action:** `upload`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** change-style permission usually requires seeing the current state first.

#### `student.student_documents.upload`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `student.student_identity.validate`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `student.enrollment_history.track`
- **Action:** `add`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student.parent_guardian_information.add`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `student.data_privacy_rules.enforce`
- **Action:** `search`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `student.student_records.search`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student.student_data.export`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student.student_record_changes.log`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Staff Management

#### `staff.staff_profile.create`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `staff.staff_role.assign`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `staff.staff_department.assign`
- **Action:** `activate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `staff.staff_account.activate`
- **Action:** `deactivate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `staff.staff_account.deactivate`
- **Action:** `transfer`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `staff.staff_department.transfer`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `staff.staff_activity.track`
- **Action:** `manage`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `staff.staff_permissions.manage`
- **Action:** `upload`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** change-style permission usually requires seeing the current state first.

#### `staff.staff_documents.upload`
- **Action:** `search`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `staff.staff_records.search`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `staff.staff_data.export`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `staff.staff_actions.log`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `staff.role_constraints.enforce`
- **Action:** `restore`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `staff.staff_account.restore`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `staff.staff_record.archive`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

### Academic Structure (Classes, Programs)

#### `academic_structure.academic_program.create`
- **Action:** `create`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_structure.department.create`
- **Action:** `create`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_structure.class.create`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_structure.class_program.assign`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `academic_structure.teacher_class.assign`
- **Action:** `define`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `academic_structure.term_structure.define`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_structure.academic_calendar.update`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `academic_structure.academic_structure.archive`
- **Action:** `clone`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `academic_structure.academic_structures.clone`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_structure.structure_dependencies.validate`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_structure.academic_configuration.lock`
- **Action:** `rollover`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `academic_structure.academic_year.rollover`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_structure.structural_changes.track`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `academic_structure.academic_setup.export`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `academic_structure.academic_modifications.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

## TIER 3 — ACADEMIC OPERATIONS (CONTINUED)

### Attendance Management

#### `attendance.attendance_rules.configure`
- **Action:** `define`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `attendance.attendance_types.define`
- **Action:** `load`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `attendance.class_attendance_roster.load`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `attendance.student_attendance.record`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `attendance.attendance_records.edit`
- **Action:** `prevent`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `attendance.duplicate_attendance_entries.prevent`
- **Action:** `auto_mark`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `attendance.unrecorded_attendance.auto_mark`
- **Action:** `sync`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `attendance.attendance_student_profile.sync`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `attendance.parents_absence.notify`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `attendance.attendance_summary.generate`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `attendance.attendance_anomalies.detect`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `attendance.attendance_records.lock`
- **Action:** `reopen`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `attendance.locked_attendance.reopen`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `attendance.attendance_history.track`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `attendance.attendance_reports.export`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `attendance.role_based_attendance_access.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `attendance.attendance_changes.audit`
- **Action:** `support`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `attendance.bulk_attendance_updates.support`
- **Action:** `support`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

### Gradebook & Assessments (Future Project)

#### `gradebook.assessment_type.create`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `gradebook.grading_scheme.configure`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `gradebook.assessments_classes.assign`
- **Action:** `enter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `gradebook.student_scores.enter`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `gradebook.submitted_scores.edit`
- **Action:** `calculate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `gradebook.totals.calculate`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `gradebook.weighted_grading_rules.apply`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `gradebook.score_ranges.validate`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `gradebook.gradebook.lock`
- **Action:** `unlock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `gradebook.gradebook.unlock`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `gradebook.report_cards.generate`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `gradebook.term_results.approve`
- **Action:** `publish`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `gradebook.results_students.publish`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `gradebook.parents_results.notify`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `gradebook.grade_history.track`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `gradebook.missing_grades.detect`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `gradebook.grade_reports.export`
- **Action:** `support`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `gradebook.continuous_assessment.support`
- **Action:** `support`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `gradebook.exam_assessments.support`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `gradebook.grade_modifications.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Academic Calendar & Timetables

#### `academic_calendar.academic_session.create`
- **Action:** `define`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_calendar.academic_terms.define`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_calendar.school_calendar.configure`
- **Action:** `set`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `academic_calendar.holidays_and_breaks.set`
- **Action:** `create`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `academic_calendar.class_timetable.create`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_calendar.subjects_time_slots.assign`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `academic_calendar.teachers_timetable.assign`
- **Action:** `prevent`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `academic_calendar.scheduling_conflicts.prevent`
- **Action:** `publish`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_calendar.timetable.publish`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `academic_calendar.timetable.update`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `academic_calendar.timetable.update`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `academic_calendar.users_schedule_changes.notify`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_calendar.academic_calendar.lock`
- **Action:** `clone`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `academic_calendar.academic_calendar.clone`
- **Action:** `rollover`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `academic_calendar.calendar_new_year.rollover`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_calendar.daily_schedule.view`
- **Action:** `sync`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `academic_calendar.calendar_attendance.sync`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `academic_calendar.timetable.export`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `academic_calendar.calendar_changes.track`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `academic_calendar.role_based_editing.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `academic_calendar.calendar_updates.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

## TIER 4 — FINANCIAL OPERATIONS

### Billing & Fees Management

#### `finance.billing.fee_item.create`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `finance.billing.fee_structure.configure`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.billing.fees_classes.assign`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.billing.fees_students.assign`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.billing.student_invoices.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.billing.bulk_invoices.generate`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.billing.invoice_items.edit`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.billing.late_fee_rules.apply`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.billing.discounts.apply`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.billing.scholarships.apply`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.billing.payment_deadlines.configure`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.billing.invoice_status.track`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.billing.issued_invoices.lock`
- **Action:** `cancel`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `finance.billing.invoices.cancel`
- **Action:** `reissue`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `finance.billing.invoices.reissue`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `finance.billing.payers_invoices.notify`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.billing.fee_history.track`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.billing.billing_reports.export`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.billing.billing_permissions.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.billing.billing_actions.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Payments & Reconciliation

#### `finance.payment.payment_gateways.configure`
- **Action:** `enable`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.payment.payment_channels.enable`
- **Action:** `process`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.payment.online_payments.process`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.payment.offline_payments.record`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.payment.payment_receipts.generate`
- **Action:** `match`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.payment.payments_invoices.match`
- **Action:** `handle`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.payment.partial_payments.handle`
- **Action:** `handle`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.payment.overpayments.handle`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.payment.failed_payments.detect`
- **Action:** `retry`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.payment.failed_payments.retry`
- **Action:** `reconcile`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.payment.gateway_transactions.reconcile`
- **Action:** `reconcile`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `finance.payment.bank_transfers.reconcile`
- **Action:** `process`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `finance.payment.refund_requests.process`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.payment.refunds.approve`
- **Action:** `execute`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `finance.payment.refunds.execute`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.payment.payment_status.track`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.payment.users_payment_events.notify`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.payment.payment_history.export`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.payment.payment_approvals.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.payment.payment_transactions.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Finance Ledger & Reporting

#### `finance.ledger.chart_accounts.configure`
- **Action:** `create`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.ledger.ledger_accounts.create`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `finance.ledger.financial_transactions.record`
- **Action:** `post`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.ledger.billing_entries.post`
- **Action:** `post`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `finance.ledger.payment_entries.post`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `finance.ledger.expense_entries.record`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.ledger.trial_balance.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.ledger.income_statement.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.ledger.balance_sheet.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.ledger.cash_flow_report.generate`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.ledger.outstanding_balances.track`
- **Action:** `filter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.ledger.financial_reports.filter`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.ledger.financial_statements.export`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.ledger.financial_periods.lock`
- **Action:** `reopen`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `finance.ledger.financial_periods.reopen`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `finance.ledger.ledger_adjustments.track`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.ledger.ledger_integrity.validate`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.ledger.approval_controls.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.ledger.financial_records.audit`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.ledger.financial_reports.schedule`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

### Discounts, Refunds & Adjustments (Future Project)

#### `finance.adjustment.discount_policy.create`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `finance.adjustment.discounts_students.assign`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.adjustment.bulk_discounts.apply`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.adjustment.discount_eligibility.validate`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.adjustment.discount_requests.approve`
- **Action:** `revoke`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `finance.adjustment.discounts.revoke`
- **Action:** `initiate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.adjustment.refund_request.initiate`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `finance.adjustment.refund_amount.validate`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.adjustment.refund_workflow.approve`
- **Action:** `execute`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `finance.adjustment.refund_payment.execute`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `finance.adjustment.billing_adjustments.apply`
- **Action:** `reverse`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `finance.adjustment.financial_entries.reverse`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `finance.adjustment.adjustment_history.track`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.adjustment.stakeholders_adjustments.notify`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.adjustment.adjustment_limits.enforce`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.adjustment.adjustments_post_approval.lock`
- **Action:** `prevent`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `finance.adjustment.duplicate_refunds.prevent`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `finance.adjustment.adjustment_reports.export`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.adjustment.discount_actions.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `finance.adjustment.refund_activities.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

## TIER 5 — PROCUREMENT & ASSETS

### Vendor Management (Future Project)

#### `procurement.vendor.vendor.register`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `procurement.vendor.vendor_registration.approve`
- **Action:** `categorize`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `procurement.vendor.vendors.categorize`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.vendor_profile.update`
- **Action:** `deactivate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `procurement.vendor.vendor.deactivate`
- **Action:** `rate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `procurement.vendor.vendor_performance.rate`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.vendor.vendor_history.track`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.vendor_contracts.assign`
- **Action:** `upload`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `procurement.vendor.vendor_documents.upload`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `procurement.vendor.vendor_compliance.validate`
- **Action:** `search`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.vendor.vendor_directory.search`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.vendor_list.export`
- **Action:** `detect`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.duplicate_vendors.detect`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.vendor_records.lock`
- **Action:** `restore`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `procurement.vendor.vendor_account.restore`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `procurement.vendor.vendor_spend_summary.view`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.vendor_payment_status.track`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.vendor.vendor_changes.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Procurement Requests & Approvals

#### `procurement.request.purchase_request.create`
- **Action:** `edit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `procurement.request.purchase_request.edit`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `procurement.request.budget_availability.validate`
- **Action:** `submit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.request.request_approval.submit`
- **Action:** `route`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `procurement.request.approval_workflow.route`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `procurement.request.purchase_request.approve`
- **Action:** `reject`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `procurement.request.purchase_request.create`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `procurement.request.purchase_request.reject`
- **Action:** `escalate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `procurement.request.purchase_request.create`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `procurement.request.high_value_requests.escalate`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** approval/review flow usually depends on an existing request or reviewable record.

#### `procurement.request.approval_status.track`
- **Action:** `add`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.request.approval_comments.add`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `procurement.request.spending_limits.enforce`
- **Action:** `cancel`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.request.purchase_request.cancel`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `procurement.request.purchase_request.create`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `procurement.request.request_stakeholders.notify`
- **Action:** `convert`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.request.request_po.convert`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.request.request_history.track`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.request.request_data.export`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.request.role_based_approvals.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.request.request_actions.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Purchase Orders & Delivery

#### `procurement.purchase_order.purchase_order.generate`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `procurement.purchase_order.vendor_po.assign`
- **Action:** `send`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `procurement.purchase_order.po_vendor.send`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.purchase_order.po_status.update`
- **Action:** `acknowledge`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `procurement.purchase_order.po_receipt.acknowledge`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.purchase_order.delivery_timeline.track`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.purchase_order.goods_received.record`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `procurement.purchase_order.delivered_quantity.validate`
- **Action:** `validate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.purchase_order.delivered_quality.validate`
- **Action:** `reject`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.purchase_order.delivered_items.reject`
- **Action:** `approve`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `procurement.purchase_order.delivery_completion.approve`
- **Action:** `trigger`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
  - `system.role_request.approve`
- **Why:** approval/review flow usually depends on an existing request or reviewable record; mutation should be captured by audit logging.

#### `procurement.purchase_order.invoice_matching.trigger`
- **Action:** `close`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `procurement.purchase_order.purchase_order.close`
- **Action:** `cancel`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `procurement.purchase_order.purchase_order.cancel`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `procurement.purchase_order.po_documents.export`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.purchase_order.po_history.track`
- **Action:** `notify`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `procurement.purchase_order.delivery_stakeholders.notify`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `procurement.purchase_order.po_changes.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Inventory & Asset Tracking

#### `inventory.inventory_item.register`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `inventory.inventory_category.assign`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `inventory.inventory_quantity.track`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `inventory.stock_levels.update`
- **Action:** `set`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `inventory.reorder_thresholds.set`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `inventory.stock_alerts.generate`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `inventory.asset_acquisition.record`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `inventory.asset_location.assign`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `inventory.asset_depreciation.track`
- **Action:** `record`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `inventory.asset_disposal.record`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `inventory.inventory_movements.audit`
- **Action:** `perform`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `inventory.stock_reconciliation.perform`
- **Action:** `lock`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `inventory.inventory_records.lock`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `inventory.inventory_reports.export`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `inventory.asset_history.track`
- **Action:** `assign`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `inventory.assets_departments.assign`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `inventory.inventory_permissions.enforce`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `inventory.asset_records.archive`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

## TIER 6 — COMMUNICATION & ENGAGEMENT

### Messaging & Notifications

#### `communication.internal_message.send`
- **Action:** `send`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.bulk_notifications.send`
- **Action:** `send`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.email_notifications.send`
- **Action:** `send`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.sms_notifications.send`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.notification_templates.configure`
- **Action:** `attach`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `communication.files_messages.attach`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** mutation should be captured by audit logging.

#### `communication.message_delivery.track`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `communication.message_history.view`
- **Action:** `filter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `communication.messages_by_type.filter`
- **Action:** `reply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `communication.messages.reply`
- **Action:** `mute`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.notification_threads.mute`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** state-changing action depends on the target record already existing.

#### `communication.notifications.schedule`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.communication_permissions.enforce`
- **Action:** `log`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `communication.communication_events.log`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `communication.message_activity.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Student Portal

#### `student_portal.student_login.create`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** creation-style action usually sits inside a parent module/resource context; mutation should be captured by audit logging.

#### `student_portal.class_timetable.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student_portal.attendance_summary.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student_portal.academic_results.view`
- **Action:** `access`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student_portal.learning_materials.access`
- **Action:** `receive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `student_portal.announcements.receive`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `student_portal.fee_status.view`
- **Action:** `download`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student_portal.receipts.download`
- **Action:** `message`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student_portal.teachers.message`
- **Action:** `update`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `student_portal.profile_details.update`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `student_portal.academic_progress.track`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `student_portal.student_permissions.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `student_portal.student_activity.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

## TIER 7 — REPORTING & ANALYTICS

### Dashboards & KPIs

#### `analytics.dashboard.institution_overview_dashboard.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.academic_performance_kpis.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.attendance_kpis.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.financial_kpis.view`
- **Action:** `view`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.procurement_kpis.view`
- **Action:** `filter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.dashboard_metrics.filter`
- **Action:** `drilldown`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.kpis.drilldown`
- **Action:** `compare`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.period_performance.compare`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.dashboard_widgets.configure`
- **Action:** `save`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `analytics.dashboard.custom_dashboards.save`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** change-style permission usually requires seeing the current state first.

#### `analytics.dashboard.dashboard_access_control.enforce`
- **Action:** `refresh`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `analytics.dashboard.dashboard_data.refresh`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `analytics.dashboard.dashboard_views.export`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.dashboard_usage.track`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `analytics.dashboard.dashboard_changes.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Operational Reports

#### `reporting.operational.attendance_reports.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `reporting.operational.academic_reports.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `reporting.operational.financial_reports.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `reporting.operational.procurement_reports.generate`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `reporting.operational.user_activity_reports.generate`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `reporting.operational.report_generation.schedule`
- **Action:** `filter`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `reporting.operational.report_parameters.filter`
- **Action:** `preview`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `reporting.operational.reports.preview`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `reporting.operational.reports_pdf.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `reporting.operational.reports_excel.export`
- **Action:** `share`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `reporting.operational.reports_email.share`
- **Action:** `archive`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `reporting.operational.generated_reports.archive`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** state-changing action depends on the target record already existing; mutation should be captured by audit logging.

#### `reporting.operational.report_history.track`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `reporting.operational.report_access_control.enforce`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `reporting.operational.report_generation.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

### Export & Data Access

#### `data_export.student_data.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.staff_data.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.attendance_data.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.academic_results.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.financial_records.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.procurement_records.export`
- **Action:** `export`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.audit_logs.export`
- **Action:** `configure`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.download.secure_link.generate`
  - `system.export.expiry.enforce`
  - `system.data_masking.apply`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.export_formats.configure`
- **Action:** `enforce`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `data_export.data_access_permissions.enforce`
- **Action:** `apply`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `data_export.data_masking_rules.apply`
- **Action:** `schedule`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
  - `system.audit.write`
- **Why:** change-style permission usually requires seeing the current state first; mutation should be captured by audit logging.

#### `data_export.automated_exports.schedule`
- **Action:** `generate`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** foundational platform guards apply.

#### `data_export.secure_download_links.generate`
- **Action:** `track`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** creation-style action usually sits inside a parent module/resource context.

#### `data_export.export_requests.track`
- **Action:** `expire`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

#### `data_export.export_files.expire`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** state-changing action depends on the target record already existing.

#### `data_export.data_access_events.audit`
- **Action:** `audit`
- **Depends on:**
  - `system.authenticated.access`
  - `system.api.access`
  - `system.tenant_context.require`
  - `system.tenant_boundary.enforce`
- **Why:** read/export-style permission usually assumes visibility of the same resource.

## Summary

- Total permissions mapped: **505**
- Permissions with at least one inferred dependency: **503**
- Standalone or root-level permissions: **2**

Use this file beside the main permission registry so your team can read both the **meaning** of each key and the **relationship** between keys.